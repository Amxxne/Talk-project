"""
watcher.py — Surveillance du dossier SafeSync en temps réel

La bibliothèque watchdog s'appuie sur les événements du système
d'exploitation (inotify sur Linux, FSEvents sur macOS, ReadDirectoryChangesW
sur Windows) pour détecter les changements SANS faire de polling.

Polling = vérifier toutes les X secondes si quelque chose a changé → lent, coûteux
Événements OS = le noyau te prévient instantanément → efficace
"""

import os
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ─────────────────────────────────────────────────────────────
#  PROBLÈME : L'EFFET "DOUBLON"
#
#  Quand tu sauvegardes un fichier dans un éditeur, l'OS génère
#  souvent PLUSIEURS événements dans la même seconde :
#    1. modified → fichier temporaire créé
#    2. created  → fichier final écrit
#    3. modified → métadonnées mises à jour
#
#  Sans protection, on enverrait le même fichier 3 fois.
#  Solution : "debounce" — on attend un court délai après
#  le dernier événement avant d'agir.
# ─────────────────────────────────────────────────────────────

DEBOUNCE_DELAY = 1.0   # secondes à attendre après le dernier événement


class SafeSyncHandler(FileSystemEventHandler):
    """
    Gestionnaire d'événements du système de fichiers.

    watchdog appelle nos méthodes on_created, on_modified,
    on_deleted automatiquement depuis son propre thread interne.
    """

    def __init__(self, sync_client, sync_folder):
        """
        sync_client  : instance de SyncClient (pour upload/delete)
        sync_folder  : chemin du dossier surveillé (pour les ignorer)
        """
        super().__init__()
        self.sync_client = sync_client
        self.sync_folder = os.path.abspath(sync_folder)

        # Dictionnaire de debounce : { filepath → timer }
        # Chaque fichier a son propre timer indépendant
        self._debounce_timers = {}
        self._debounce_lock   = threading.Lock()

    # ─────────────────────────────────────────────────────────
    #  FILTRES — fichiers à ignorer
    # ─────────────────────────────────────────────────────────

    def _should_ignore(self, path):
        """
        Retourne True si on doit ignorer cet événement.

        On ignore :
          - Les dossiers (on sync seulement les fichiers)
          - Les fichiers temporaires des éditeurs (.swp, ~, .tmp)
          - Les fichiers cachés (.DS_Store, thumbs.db)
          - Les fichiers en cours de download (évite les boucles !)
        """
        if os.path.isdir(path):
            return True

        filename = os.path.basename(path)

        # Fichiers temporaires courants
        ignored_patterns = [
            filename.startswith('.'),       # fichiers cachés
            filename.endswith('~'),         # backup vim/emacs
            filename.endswith('.swp'),      # fichier swap vim
            filename.endswith('.tmp'),      # fichiers temporaires
            filename.endswith('.part'),     # téléchargement partiel
            filename.startswith('~$'),      # fichiers Office temporaires
        ]

        return any(ignored_patterns)

    # ─────────────────────────────────────────────────────────
    #  DEBOUNCE
    # ─────────────────────────────────────────────────────────

    def _debounce(self, filepath, action):
        """
        Annule et recrée un timer pour ce fichier.

        Si 3 événements arrivent en 0.3s pour le même fichier :
          événement 1 → timer créé (1.0s)
          événement 2 → timer annulé, nouveau timer (1.0s)
          événement 3 → timer annulé, nouveau timer (1.0s)
          ... 1.0s de silence ...
          action exécutée UNE SEULE FOIS ✓
        """
        with self._debounce_lock:
            # Annule le timer précédent s'il existe
            if filepath in self._debounce_timers:
                self._debounce_timers[filepath].cancel()

            # Crée un nouveau timer
            timer = threading.Timer(
                DEBOUNCE_DELAY,
                self._execute_action,
                args=[filepath, action]
            )
            self._debounce_timers[filepath] = timer
            timer.start()

    def _execute_action(self, filepath, action):
        """Appelé par le timer après le délai de debounce."""
        with self._debounce_lock:
            # Nettoie le timer du dictionnaire
            self._debounce_timers.pop(filepath, None)

        if action == 'upload':
            # Vérifie une dernière fois que le fichier existe encore
            # (il aurait pu être supprimé pendant le délai)
            if os.path.exists(filepath):
                self.sync_client.upload_file(filepath)
            else:
                print(f"[Watcher] Fichier disparu avant l'envoi : {filepath}")

        elif action == 'delete':
            filename = os.path.basename(filepath)
            self.sync_client.delete_file(filename)

    # ─────────────────────────────────────────────────────────
    #  CALLBACKS WATCHDOG
    #
    #  Ces méthodes sont appelées automatiquement par watchdog
    #  depuis son thread interne quand l'OS signale un événement.
    # ─────────────────────────────────────────────────────────

    def on_created(self, event):
        """Un nouveau fichier est apparu dans le dossier."""
        if self._should_ignore(event.src_path):
            return

        print(f"[Watcher] Création détectée : {event.src_path}")
        self._debounce(event.src_path, 'upload')

    def on_modified(self, event):
        """Un fichier existant a été modifié."""
        if self._should_ignore(event.src_path):
            return

        print(f"[Watcher] Modification détectée : {event.src_path}")
        self._debounce(event.src_path, 'upload')

    def on_deleted(self, event):
        """Un fichier a été supprimé."""
        if self._should_ignore(event.src_path):
            return

        print(f"[Watcher] Suppression détectée : {event.src_path}")
        # Pas de debounce pour les suppressions : c'est définitif
        filename = os.path.basename(event.src_path)
        self.sync_client.delete_file(filename)

    def on_moved(self, event):
        """
        Un fichier a été renommé ou déplacé.
        Pour SafeSync : renommer = supprimer l'ancien + créer le nouveau.
        """
        if self._should_ignore(event.src_path):
            return

        print(f"[Watcher] Déplacement : {event.src_path} → {event.dest_path}")

        # Supprime l'ancien nom sur le serveur
        old_filename = os.path.basename(event.src_path)
        self.sync_client.delete_file(old_filename)

        # Upload le fichier sous son nouveau nom
        if not self._should_ignore(event.dest_path):
            self._debounce(event.dest_path, 'upload')


# ─────────────────────────────────────────────────────────────
#  CLASSE PRINCIPALE
# ─────────────────────────────────────────────────────────────

class FolderWatcher:
    """
    Lance et gère l'Observer watchdog.

    L'Observer est le thread qui communique avec l'OS
    pour recevoir les événements du système de fichiers.
    """

    def __init__(self, sync_folder, sync_client):
        self.sync_folder = sync_folder
        self.sync_client = sync_client
        self.observer    = None

        # Crée le dossier s'il n'existe pas
        os.makedirs(sync_folder, exist_ok=True)

    def start(self):
        """Démarre la surveillance du dossier."""
        handler = SafeSyncHandler(self.sync_client, self.sync_folder)

        self.observer = Observer()

        # schedule() : "surveille ce dossier avec ce handler"
        # recursive=False : on ne surveille pas les sous-dossiers
        self.observer.schedule(handler, self.sync_folder, recursive=False)
        self.observer.start()

        print(f"[Watcher] Surveillance démarrée sur : {self.sync_folder}")

    def stop(self):
        """Arrête proprement la surveillance."""
        if self.observer:
            self.observer.stop()
            self.observer.join()   # Attend la fin du thread observer
            print("[Watcher] Surveillance arrêtée.")
