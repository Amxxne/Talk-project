#ifndef SERVER_H
#define SERVER_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

/* =========================================================
 *  CONSTANTES
 * ========================================================= */

#define PORT            5000      // Port d'écoute du serveur
#define MAX_CLIENTS     32        // Nombre max de clients simultanés
#define BUFFER_SIZE     8192      // Taille des chunks de transfert (8 Ko)
#define MAX_FILENAME    256       // Longueur max d'un nom de fichier
#define HASH_SIZE       64        // Taille du hash SHA-256 en hex (64 chars)
#define STORAGE_DIR     "./storage" // Dossier où les fichiers sont stockés

/* =========================================================
 *  CODES OPÉRATION (le "langage" du protocole)
 *
 *  Quand un client envoie un paquet, le premier octet
 *  indique ce qu'il veut faire. C'est notre protocole
 *  binaire maison.
 * ========================================================= */

#define OP_UPLOAD       0x01  // Le client envoie un fichier au serveur
#define OP_DOWNLOAD     0x02  // Le client demande un fichier
#define OP_DELETE       0x03  // Le client demande la suppression d'un fichier
#define OP_NOTIFY       0x04  // Le serveur notifie un client d'un changement
#define OP_ACK          0x05  // Accusé de réception (tout s'est bien passé)
#define OP_ERROR        0x06  // Signalement d'une erreur

/* =========================================================
 *  STRUCTURE : EN-TÊTE DE PAQUET (Header)
 *
 *  Chaque message échangé commence par ce header.
 *  __attribute__((packed)) dit au compilateur de ne pas
 *  ajouter de "padding" (octets de remplissage) entre
 *  les champs — crucial pour un protocole réseau !
 * ========================================================= */

typedef struct __attribute__((packed)) {
    uint8_t  opcode;                // Code opération (OP_UPLOAD, etc.)
    uint16_t filename_len;          // Longueur du nom de fichier
    char     filename[MAX_FILENAME];// Nom du fichier
    uint64_t file_size;             // Taille totale du fichier en octets
    char     hash[HASH_SIZE + 1];   // Hash SHA-256 du fichier (vérification)
} PacketHeader;

/* =========================================================
 *  STRUCTURE : CLIENT CONNECTÉ
 *
 *  On garde une "fiche" pour chaque client connecté.
 *  Le tableau global clients[] contient toutes ces fiches.
 * ========================================================= */

typedef struct {
    int      socket_fd;         // Descripteur de la socket de ce client
    int      active;            // 1 = connecté, 0 = slot libre
    char     ip[INET_ADDRSTRLEN]; // Adresse IP du client (pour les logs)
    pthread_t thread;           // Le thread dédié à ce client
} ClientInfo;

/* =========================================================
 *  VARIABLES GLOBALES (déclarées extern, définies dans main.c)
 *
 *  extern = "cette variable existe quelque part ailleurs,
 *  fais-moi confiance". Ça permet de partager des données
 *  entre plusieurs fichiers .c
 * ========================================================= */

extern ClientInfo clients[MAX_CLIENTS]; // Tableau de tous les clients
extern pthread_mutex_t clients_mutex;   // Verrou pour accéder au tableau
                                        // sans collision entre threads

/* =========================================================
 *  SIGNATURES DES FONCTIONS
 * ========================================================= */

// Initialise la socket serveur et se met en écoute
int  server_init(int port);

// Boucle principale : accepte les nouvelles connexions
void server_run(int server_fd);

// Fonction exécutée par chaque thread client
void *client_handler(void *arg);

// Notifie tous les autres clients qu'un fichier a changé
void broadcast_notify(int sender_fd, const char *filename, uint8_t action);

// Trouve un slot libre dans le tableau clients[]
int  find_free_slot(void);

#endif // SERVER_H
