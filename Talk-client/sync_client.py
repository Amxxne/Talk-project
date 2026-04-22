"""
sync_client.py — Gestion de la connexion TCP et du protocole SafeSync

Ce fichier est le "miroir Python" de client_handler.c.
Il parle exactement le même protocole binaire que le serveur C.
"""

import socket
import struct
import hashlib
import os
import threading
import queue
import time

# ─────────────────────────────────────────────────────────────
#  CONSTANTES — doivent être identiques à server.h !
# ─────────────────────────────────────────────────────────────

PORT         = 5003
BUFFER_SIZE  = 8192   # Taille des chunks (8 Ko)
MAX_FILENAME = 256
HASH_SIZE    = 64

# Opcodes (même valeurs qu'en C)
OP_UPLOAD   = 0x01
OP_DOWNLOAD = 0x02
OP_DELETE   = 0x03
OP_NOTIFY   = 0x04
OP_ACK      = 0x05
OP_ERROR    = 0x06

# ─────────────────────────────────────────────────────────────
#  FORMAT DU HEADER — struct.pack/unpack remplace __attribute__((packed))
#
#  En C on avait :
#    uint8_t  opcode;          → 'B' (1 octet non signé)
#    uint16_t filename_len;    → 'H' (2 octets non signés)
#    char     filename[256];   → '256s' (256 octets bruts)
#    uint64_t file_size;       → 'Q' (8 octets non signés)
#    char     hash[65];        → '65s' (65 octets bruts)
#
#  '!' = big-endian réseau (network byte order)
#  C'est l'équivalent de htons()/htonl() en C
# ─────────────────────────────────────────────────────────────

HEADER_FORMAT = '!B H 256s Q 65s'
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)  # = 334 octets


def build_header(opcode, filename, file_size, file_hash=''):
    """
    Construit un header binaire prêt à être envoyé sur le réseau.
    Équivalent de remplir un PacketHeader{} en C.
    """
    name_bytes = filename.encode('utf-8')
    filename_len = len(name_bytes)

    # On pad le nom de fichier à exactement 256 octets
    # (ljust = left-justify, complète avec des \x00)
    name_padded = name_bytes.ljust(MAX_FILENAME, b'\x00')

    hash_bytes = file_hash.encode('utf-8').ljust(HASH_SIZE + 1, b'\x00')

    return struct.pack(
        HEADER_FORMAT,
        opcode,
        filename_len,
        name_padded,
        file_size,
        hash_bytes
    )


def parse_header(raw_bytes):
    """
    Décode un header binaire reçu du serveur.
    Retourne un dict avec les champs nommés.
    """
    opcode, filename_len, filename_raw, file_size, hash_raw = \
        struct.unpack(HEADER_FORMAT, raw_bytes)

    # Decode et nettoie les chaînes (enlève les \x00 de padding)
    filename  = filename_raw[:filename_len].decode('utf-8', errors='replace')
    file_hash = hash_raw.rstrip(b'\x00').decode('utf-8', errors='replace')

    return {
        'opcode':       opcode,
        'filename_len': filename_len,
        'filename':     filename,
        'file_size':    file_size,
        'hash':         file_hash,
    }


def sha256_of_file(filepath):
    """
    Calcule le hash SHA-256 d'un fichier par chunks.
    Même principe que la lecture par chunks : ne charge jamais
    tout le fichier en RAM.
    """
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(BUFFER_SIZE):   # := est le "walrus operator" Python 3.8+
            h.update(chunk)
    return h.hexdigest()   # Retourne 64 caractères hexadécimaux


# ─────────────────────────────────────────────────────────────
#  CLASSE PRINCIPALE
# ─────────────────────────────────────────────────────────────

class SyncClient:
    """
    Gère la connexion TCP avec le serveur SafeSync.

    Deux threads tournent en parallèle :
      - _sender_thread   : vide la file d'attente et envoie les fichiers
      - _listener_thread : écoute les notifications du serveur

    La file d'attente (queue) permet la résilience :
    si le serveur est hors ligne, les tâches s'accumulent
    et sont envoyées dès que la connexion revient.
    """

    def __init__(self, server_host, sync_folder, on_notify=None, on_status=None):
        """
        server_host  : IP ou hostname du serveur
        sync_folder  : dossier local à synchroniser
        on_notify    : callback appelé quand le serveur notifie un changement
                       signature : on_notify(filename)
        on_status    : callback pour mettre à jour la GUI
                       signature : on_status(message)
        """
        self.server_host  = server_host
        self.server_port  = PORT
        self.sync_folder  = sync_folder
        self.on_notify    = on_notify
        self.on_status    = on_status
        self.on_connected = None  # sera défini depuis main.py

        self.sock         = None
        self.connected    = False
        self._lock        = threading.Lock()  # Protège self.sock (comme le mutex en C)

        # File d'attente des tâches à envoyer
        # Queue est thread-safe nativement en Python
        self.upload_queue = queue.Queue()

        # Démarre les threads en arrière-plan
        self._start_threads()

    def _log(self, msg):
        print(f"[Client] {msg}")
        if self.on_status:
            self.on_status(msg)

    # ─────────────────────────────────────────────────────────
    #  CONNEXION / RECONNEXION
    # ─────────────────────────────────────────────────────────

    def _connect(self):
        """Tente de se connecter au serveur. Retourne True si succès."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)  # Timeout de 5s pour la connexion
            sock.connect((self.server_host, self.server_port))
            sock.settimeout(None)  # Connexion établie : retire le timeout

            with self._lock:
                self.sock      = sock
                self.connected = True

            self._log(f"Connecté à {self.server_host}:{self.server_port}")
            if self.on_connected:
                self.on_connected(True)
            return True

        except (ConnectionRefusedError, OSError) as e:
            self._log(f"Connexion impossible : {e}")
            self.connected = False
            return False

    def _disconnect(self):
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
                self.sock      = None
                self.connected = False
                if self.on_connected:
                    self.on_connected(False)

    def _recv_all(self, n):
        """
        Reçoit exactement n octets — identique à recv_all() en C.
        Retourne les bytes reçus, ou lève une exception si la
        connexion est coupée.
        """
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Connexion fermée par le serveur")
            data += chunk
        return data

    def _send_all(self, data):
        """Envoie exactement len(data) octets."""
        self.sock.sendall(data)  # sendall() fait la boucle pour nous en Python

    # ─────────────────────────────────────────────────────────
    #  OPÉRATIONS PROTOCOLE
    # ─────────────────────────────────────────────────────────

    def upload_file(self, filepath):
        """
        Envoie un fichier au serveur.
        Ajoute la tâche dans la file d'attente (non bloquant).
        """
        self.upload_queue.put(('upload', filepath))
        self._log(f"Ajout en file : upload de '{os.path.basename(filepath)}'")

    def delete_file(self, filename):
        """Notifie le serveur qu'un fichier a été supprimé."""
        self.upload_queue.put(('delete', filename))

    def _do_upload(self, filepath):
        """Exécution réelle de l'upload (appelée par le sender thread)."""
        if not os.path.exists(filepath):
            self._log(f"Fichier introuvable, annulé : {filepath}")
            return

        filename  = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)
        file_hash = sha256_of_file(filepath)

        self._log(f"Upload de '{filename}' ({file_size} octets)...")

        # Envoie le header
        header = build_header(OP_UPLOAD, filename, file_size, file_hash)
        self._send_all(header)

        # Envoie le fichier par chunks
        with open(filepath, 'rb') as f:
            sent = 0
            while chunk := f.read(BUFFER_SIZE):
                self._send_all(chunk)
                sent += len(chunk)

        # Attend l'ACK du serveur
        ack = self._recv_all(1)
        if ack[0] == OP_ACK:
            self._log(f"'{filename}' synchronisé avec succès ✓")
        else:
            self._log(f"Erreur serveur pour '{filename}'")

    def _do_delete(self, filename):
        """Exécution réelle de la suppression."""
        self._log(f"Suppression de '{filename}' sur le serveur...")
        header = build_header(OP_DELETE, filename, 0)
        self._send_all(header)

        ack = self._recv_all(1)
        if ack[0] == OP_ACK:
            self._log(f"'{filename}' supprimé sur le serveur ✓")

    def download_file(self, filename):
        """Télécharge un fichier depuis le serveur."""
        self._log(f"Download de '{filename}'...")

        header = build_header(OP_DOWNLOAD, filename, 0)
        self._send_all(header)

        # Reçoit le header de réponse
        raw = self._recv_all(HEADER_SIZE)
        response = parse_header(raw)

        if response['opcode'] == OP_ERROR:
            self._log(f"Fichier '{filename}' introuvable sur le serveur")
            return

        file_size = response['file_size']
        dest_path = os.path.join(self.sync_folder, filename)

        # Reçoit le contenu par chunks
        with open(dest_path, 'wb') as f:
            remaining = file_size
            while remaining > 0:
                to_read = min(BUFFER_SIZE, remaining)
                chunk   = self._recv_all(to_read)
                f.write(chunk)
                remaining -= len(chunk)

        self._log(f"'{filename}' téléchargé ✓")

    # ─────────────────────────────────────────────────────────
    #  THREADS
    # ─────────────────────────────────────────────────────────

    def _sender_thread(self):
        """
        Thread qui vide la file d'attente en continu.

        Si le serveur est hors ligne :
          → on attend 5s et on réessaie (résilience)
          → les tâches restent dans la queue sans être perdues
        """
        while True:
            # get() est BLOQUANT : attend qu'une tâche arrive
            task = self.upload_queue.get()
            action, payload = task

            # Reconnexion si nécessaire
            if not self.connected:
                self._log("Tentative de reconnexion...")
                while not self._connect():
                    self._log("Serveur injoignable, nouvel essai dans 5s...")
                    time.sleep(5)
                # Relance le listener après reconnexion
                threading.Thread(target=self._listener_thread,
                                 daemon=True).start()

            try:
                if action == 'upload':
                    self._do_upload(payload)
                elif action == 'delete':
                    self._do_delete(payload)

            except (ConnectionError, OSError) as e:
                self._log(f"Connexion perdue : {e}")
                self._disconnect()
                # Remet la tâche en queue pour réessayer
                self.upload_queue.put(task)
                time.sleep(2)

            finally:
                self.upload_queue.task_done()

    def _listener_thread(self):
        """
        Thread qui écoute les notifications entrantes du serveur.

        Le serveur peut envoyer un OP_NOTIFY à tout moment
        (quand un autre client uploade un fichier).
        Ce thread tourne en arrière-plan et réagit.
        """
        self._log("Listener démarré, en attente de notifications...")

        while self.connected:
            try:
                # Lit l'opcode (1 octet) → doit être OP_NOTIFY
                raw_opcode = self._recv_all(1)
                opcode = raw_opcode[0]

                if opcode == OP_NOTIFY:
                    # Lit l'action (1 octet) : OP_UPLOAD ou OP_DELETE
                    action = self._recv_all(1)[0]

                    # Lit la longueur du nom (2 octets)
                    raw_len  = self._recv_all(2)
                    name_len = struct.unpack('!H', raw_len)[0]

                    # Lit le nom du fichier
                    filename = self._recv_all(name_len).decode('utf-8')

                    if action == OP_UPLOAD:
                        self._log(f"'{filename}' mis à jour par un collègue → téléchargement...")
                        # Télécharge automatiquement le fichier dans ~/SafeSync
                        self.download_file(filename)
                        if self.on_notify:
                            self.on_notify(filename)

                    elif action == OP_DELETE:
                        self._log(f"'{filename}' supprimé par un collègue → suppression locale...")
                        local_path = os.path.join(self.sync_folder, filename)
                        if os.path.exists(local_path):
                            os.remove(local_path)
                            self._log(f"'{filename}' supprimé localement ✓")
                        if self.on_notify:
                            self.on_notify(filename)
# Télécharge automatiquement le fichier mis à jour
                            self.download_file(filename)

            except (ConnectionError, OSError):
                self._log("Listener : connexion perdue")
                self._disconnect()
                break

    def _start_threads(self):
        """Lance les threads en mode daemon (s'arrêtent avec le programme)."""
        threading.Thread(target=self._sender_thread,
                         daemon=True, name="SafeSync-Sender").start()

        # Essaie une première connexion, puis démarre le listener
        if self._connect():
            threading.Thread(target=self._listener_thread,
                             daemon=True, name="SafeSync-Listener").start()
        else:
            self._log("Démarrage hors-ligne, les fichiers seront envoyés à la reconnexion")
