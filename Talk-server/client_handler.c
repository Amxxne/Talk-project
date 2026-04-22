#include "server.h"
#include <sys/stat.h>
#include <errno.h>

/* Conversion portable big-endian → little-endian.
   Fonctionne sur macOS ET Linux sans dépendance. */
static uint16_t net_to_host16(uint16_t x) {
    return (uint16_t)((x >> 8) | (x << 8));
}
static uint64_t net_to_host64(uint64_t x) {
    return ((uint64_t)net_to_host16(x & 0xFFFF) << 48) |
           ((uint64_t)net_to_host16((x >> 16) & 0xFFFF) << 32) |
           ((uint64_t)net_to_host16((x >> 32) & 0xFFFF) << 16) |
           ((uint64_t)net_to_host16((x >> 48) & 0xFFFF));
}

/* =========================================================
 *  POURQUOI recv() ET send() ET PAS read()/write() ?
 *
 *  Sur TCP, les données arrivent en flux continu.
 *  Si tu envoies 100 octets, recv() peut en retourner
 *  60 la première fois, puis 40 la deuxième. C'est normal.
 *  On utilise des fonctions "robustes" qui bouclement
 *  jusqu'à avoir reçu/envoyé EXACTEMENT ce qu'on veut.
 * ========================================================= */

/* ---------------------------------------------------------
 *  recv_all() — Reçoit exactement 'len' octets
 * --------------------------------------------------------- */
static int recv_all(int fd, void *buf, size_t len) {
    size_t received = 0;
    char  *ptr      = (char *)buf;

    while (received < len) {
        ssize_t n = recv(fd, ptr + received, len - received, 0);
        if (n <= 0) {
            // n == 0 : client déconnecté proprement
            // n < 0  : erreur réseau
            return -1;
        }
        received += n;
    }
    return 0; // Succès
}

/* ---------------------------------------------------------
 *  send_all() — Envoie exactement 'len' octets
 * --------------------------------------------------------- */
static int send_all(int fd, const void *buf, size_t len) {
    size_t sent = 0;
    const char *ptr = (const char *)buf;

    while (sent < len) {
        ssize_t n = send(fd, ptr + sent, len - sent, 0);
        if (n <= 0) return -1;
        sent += n;
    }
    return 0;
}

/* =========================================================
 *  send_ack() / send_error() — Réponses simples au client
 * ========================================================= */

static void send_ack(int fd) {
    uint8_t ack = OP_ACK;
    send_all(fd, &ack, sizeof(ack));
}

static void send_error(int fd, const char *msg) {
    uint8_t  code     = OP_ERROR;
    uint16_t msg_len  = (uint16_t)strlen(msg);

    send_all(fd, &code,    sizeof(code));
    send_all(fd, &msg_len, sizeof(msg_len));
    send_all(fd, msg,      msg_len);
}

/* =========================================================
 *  broadcast_notify() — Notifie tous les AUTRES clients
 *
 *  Quand le client A uploade "rapport.pdf", le serveur
 *  doit prévenir B et C que ce fichier a changé.
 *  On envoie juste l'opcode + le nom du fichier.
 * ========================================================= */

void broadcast_notify(int sender_fd, const char *filename, uint8_t action) {
    uint8_t  opcode   = OP_NOTIFY;
    uint16_t name_len = (uint16_t)strlen(filename);

    pthread_mutex_lock(&clients_mutex);

    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (!clients[i].active) continue;
        if (clients[i].socket_fd == sender_fd) continue;

        // Envoi : [OP_NOTIFY 1o][action 1o][name_len 2o][filename Xo]
        // 'action' = OP_UPLOAD ou OP_DELETE pour que le client sache quoi faire
        send_all(clients[i].socket_fd, &opcode,   sizeof(opcode));
        send_all(clients[i].socket_fd, &action,   sizeof(action));
        send_all(clients[i].socket_fd, &name_len, sizeof(name_len));
        send_all(clients[i].socket_fd, filename,  name_len);

        printf("[Broadcast] Notification '%s' (action=0x%02X) → client %s\n",
               filename, action, clients[i].ip);
    }

    pthread_mutex_unlock(&clients_mutex);
}

/* =========================================================
 *  handle_upload() — Reçoit un fichier du client
 *
 *  Protocole :
 *    Client → [PacketHeader] puis [chunks de BUFFER_SIZE]
 *    Serveur → [OP_ACK] si tout va bien
 *
 *  On lit par morceaux (chunks) pour ne jamais charger
 *  un fichier de 500 Mo entier en RAM.
 * ========================================================= */

static void handle_upload(int client_fd, PacketHeader *hdr) {
    // Construit le chemin de destination : ./storage/nom_fichier
    char filepath[512];
    snprintf(filepath, sizeof(filepath), "%s/%s", STORAGE_DIR, hdr->filename);

    printf("[Upload] Réception de '%s' (%llu octets) hash=%s\n",
           hdr->filename, (unsigned long long)hdr->file_size, hdr->hash);

    // Ouvre le fichier en écriture (crée ou écrase)
    // Un fichier partiel sera écrasé à la prochaine tentative
    FILE *fp = fopen(filepath, "wb");
    if (!fp) {
        perror("[ERREUR] fopen()");
        send_error(client_fd, "Impossible de créer le fichier");
        return;
    }

    // --- Lecture par chunks ---
    // On alloue un buffer fixe de 8 Ko sur la pile.
    // Peu importe si le fichier fait 500 Mo : on lit 8 Ko,
    // on écrit 8 Ko, on recommence. RAM utilisée : 8 Ko max.
    char    chunk[BUFFER_SIZE];
    uint64_t remaining = hdr->file_size;

    while (remaining > 0) {
        // Lit au maximum BUFFER_SIZE octets, ou ce qui reste
        size_t  to_read = (remaining > BUFFER_SIZE) ? BUFFER_SIZE : remaining;
        ssize_t n       = recv(client_fd, chunk, to_read, 0);

        if (n <= 0) {
            // Connexion coupée en plein milieu du transfert
            printf("[ERREUR] Transfert interrompu pour '%s'\n", hdr->filename);
            fclose(fp);
            // On laisse le fichier partiel : il sera écrasé
            // lors de la prochaine tentative (tolérance aux pannes)
            return;
        }

        fwrite(chunk, 1, n, fp);
        remaining -= n;
    }

    fclose(fp);
    printf("[Upload] '%s' reçu avec succès.\n", hdr->filename);

    // Confirme la réception au client
    send_ack(client_fd);

    // Notifie les autres clients
    broadcast_notify(client_fd, hdr->filename, OP_UPLOAD);
}

/* =========================================================
 *  handle_download() — Envoie un fichier au client
 *
 *  Protocole :
 *    Client → [PacketHeader avec filename, file_size=0]
 *    Serveur → [PacketHeader avec la vraie file_size]
 *               puis [chunks]
 * ========================================================= */

static void handle_download(int client_fd, PacketHeader *hdr) {
    char filepath[512];
    snprintf(filepath, sizeof(filepath), "%s/%s", STORAGE_DIR, hdr->filename);

    // Vérifie que le fichier existe et récupère sa taille
    struct stat st;
    if (stat(filepath, &st) < 0) {
        send_error(client_fd, "Fichier introuvable");
        return;
    }

    printf("[Download] Envoi de '%s' (%lld octets)\n",
           hdr->filename, (long long)st.st_size);

    // Prépare et envoie le header de réponse
    PacketHeader response;
    memset(&response, 0, sizeof(response));
    response.opcode      = OP_DOWNLOAD;
    response.file_size   = (uint64_t)st.st_size;
    response.filename_len = (uint16_t)strlen(hdr->filename);
    strncpy(response.filename, hdr->filename, MAX_FILENAME - 1);

    if (send_all(client_fd, &response, sizeof(response)) < 0) return;

    // Envoie le fichier par chunks
    FILE *fp = fopen(filepath, "rb");
    if (!fp) {
        perror("[ERREUR] fopen()");
        return;
    }

    char    chunk[BUFFER_SIZE];
    size_t  n;

    while ((n = fread(chunk, 1, BUFFER_SIZE, fp)) > 0) {
        if (send_all(client_fd, chunk, n) < 0) {
            printf("[ERREUR] Envoi interrompu pour '%s'\n", hdr->filename);
            break;
        }
    }

    fclose(fp);
    printf("[Download] '%s' envoyé.\n", hdr->filename);
}

/* =========================================================
 *  handle_delete() — Supprime un fichier du stockage
 * ========================================================= */

static void handle_delete(int client_fd, PacketHeader *hdr) {
    char filepath[512];
    snprintf(filepath, sizeof(filepath), "%s/%s", STORAGE_DIR, hdr->filename);

    if (remove(filepath) == 0) {
        printf("[Delete] '%s' supprimé.\n", hdr->filename);
        send_ack(client_fd);
        broadcast_notify(client_fd, hdr->filename, OP_DELETE);
    } else {
        send_error(client_fd, "Impossible de supprimer le fichier");
    }
}

/* =========================================================
 *  client_handler() — Point d'entrée du thread client
 *
 *  Cette fonction est exécutée dans un thread séparé
 *  pour CHAQUE client connecté. Elle tourne en boucle :
 *    1. Lis le header du prochain paquet
 *    2. Dispatch vers la bonne fonction selon l'opcode
 *    3. Répète jusqu'à déconnexion
 * ========================================================= */

void *client_handler(void *arg) {
    ClientInfo *client = (ClientInfo *)arg;
    int         fd     = client->socket_fd;

    printf("[Thread] Démarrage pour client %s\n", client->ip);

    PacketHeader hdr;

    // Boucle principale du thread : lit un header, traite, recommence
    while (1) {
        // Tente de lire un header complet
        // Si recv_all retourne -1 → client déconnecté → on sort
        if (recv_all(fd, &hdr, sizeof(hdr)) < 0) {
            printf("[Thread] Client %s déconnecté.\n", client->ip);
            break;
        }

        // Convertit les champs multi-octets de network byte order (big-endian)
        // vers l'ordre natif de la machine (little-endian sur x86/ARM)
        hdr.filename_len = net_to_host16(hdr.filename_len);
        hdr.file_size    = net_to_host64(hdr.file_size);

        // Dispatch selon l'opcode reçu
        switch (hdr.opcode) {
            case OP_UPLOAD:
                handle_upload(fd, &hdr);
                break;

            case OP_DOWNLOAD:
                handle_download(fd, &hdr);
                break;

            case OP_DELETE:
                handle_delete(fd, &hdr);
                break;

            default:
                printf("[Thread] Opcode inconnu: 0x%02X\n", hdr.opcode);
                send_error(fd, "Opcode inconnu");
                break;
        }
    }

    // --- Nettoyage : libère le slot ---
    // On verrouille le mutex pour modifier le tableau partagé
    pthread_mutex_lock(&clients_mutex);
    client->active = 0;
    pthread_mutex_unlock(&clients_mutex);

    close(fd);
    printf("[Thread] Slot libéré pour %s\n", client->ip);

    return NULL;
}
