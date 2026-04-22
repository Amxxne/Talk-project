#include "server.h"
#include <sys/stat.h>
#include <signal.h>

/* =========================================================
 *  DÉFINITION DES VARIABLES GLOBALES
 *
 *  Déclarées "extern" dans server.h, elles sont DÉFINIES
 *  ici (une seule définition, plusieurs déclarations : OK)
 * ========================================================= */

ClientInfo      clients[MAX_CLIENTS];
pthread_mutex_t clients_mutex = PTHREAD_MUTEX_INITIALIZER;

/* =========================================================
 *  GESTION DU SIGNAL CTRL+C
 *
 *  Sans ça, si tu fais Ctrl+C, la socket reste "occupée"
 *  et au redémarrage tu obtiens "Address already in use".
 *  On intercepte le signal pour fermer proprement.
 * ========================================================= */

static int server_fd_global = -1;

void handle_sigint(int sig) {
    (void)sig; // Évite le warning "unused parameter"
    printf("\n[Serveur] Arrêt propre en cours...\n");

    // Ferme toutes les connexions clients actives
    pthread_mutex_lock(&clients_mutex);
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (clients[i].active) {
            close(clients[i].socket_fd);
        }
    }
    pthread_mutex_unlock(&clients_mutex);

    if (server_fd_global != -1) close(server_fd_global);
    exit(0);
}

/* =========================================================
 *  server_init() — Prépare la socket d'écoute
 *
 *  Les sockets réseau fonctionnent en plusieurs étapes :
 *  1. socket()  → crée le "point de communication"
 *  2. setsockopt() → configure les options
 *  3. bind()    → attache au port voulu
 *  4. listen()  → se met en attente de connexions
 * ========================================================= */

int server_init(int port) {
    int server_fd;
    struct sockaddr_in address;

    // --- Étape 1 : Créer la socket ---
    // AF_INET     = protocole IPv4
    // SOCK_STREAM = connexion TCP (fiable, ordonné)
    // 0           = protocole par défaut (TCP pour STREAM)
    server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) {
        perror("[ERREUR] socket()");
        return -1;
    }

    // --- Étape 2 : Option SO_REUSEADDR ---
    // Sans ça : si le serveur crash et redémarre dans les 60s,
    // bind() échoue avec "Address already in use".
    // Avec ça : on réutilise immédiatement le port.
    int opt = 1;
    if (setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        perror("[ERREUR] setsockopt()");
        close(server_fd);
        return -1;
    }

    // --- Étape 3 : Configurer l'adresse ---
    // htons() convertit le port en "network byte order"
    // (big-endian réseau vs little-endian x86)
    // INADDR_ANY = écoute sur toutes les interfaces réseau
    memset(&address, 0, sizeof(address));
    address.sin_family      = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port        = htons(port);

    // --- Étape 4 : Bind — attacher la socket au port ---
    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        perror("[ERREUR] bind()");
        close(server_fd);
        return -1;
    }

    // --- Étape 5 : Listen — file d'attente de connexions ---
    // Le "10" = backlog : nombre de connexions en attente
    // avant que le serveur les accepte (file d'attente noyau)
    if (listen(server_fd, 10) < 0) {
        perror("[ERREUR] listen()");
        close(server_fd);
        return -1;
    }

    printf("[Serveur] En écoute sur le port %d...\n", port);
    return server_fd;
}

/* =========================================================
 *  find_free_slot() — Trouve un slot libre dans clients[]
 *
 *  On DOIT verrouiller le mutex avant d'appeler cette
 *  fonction (le mutex doit être pris par l'appelant).
 * ========================================================= */

int find_free_slot(void) {
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (!clients[i].active) return i;
    }
    return -1; // Tableau plein
}

/* =========================================================
 *  server_run() — La boucle principale du serveur
 *
 *  C'est une boucle infinie qui attend des connexions.
 *  Pour CHAQUE nouveau client :
 *    1. accept() débloque et retourne une socket dédiée
 *    2. On trouve un slot libre
 *    3. On lance un thread pour gérer ce client
 *    4. On retourne attendre le prochain
 * ========================================================= */

void server_run(int server_fd) {
    struct sockaddr_in client_addr;
    socklen_t addr_len = sizeof(client_addr);

    while (1) {
        // accept() est BLOQUANT : le programme s'arrête ici
        // jusqu'à qu'un client se connecte.
        // Il retourne un nouveau fd dédié à CE client.
        // server_fd continue d'écouter les suivants.
        int client_fd = accept(server_fd,
                               (struct sockaddr *)&client_addr,
                               &addr_len);
        if (client_fd < 0) {
            perror("[ERREUR] accept()");
            continue; // On ne crash pas, on réessaie
        }

        // Récupère l'IP du client pour les logs
        char client_ip[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &client_addr.sin_addr, client_ip, INET_ADDRSTRLEN);
        printf("[Serveur] Nouvelle connexion depuis %s\n", client_ip);

        // --- Cherche un slot libre (avec verrou) ---
        pthread_mutex_lock(&clients_mutex);
        int slot = find_free_slot();

        if (slot == -1) {
            pthread_mutex_unlock(&clients_mutex);
            printf("[Serveur] Capacité max atteinte, connexion refusée.\n");
            close(client_fd);
            continue;
        }

        // Remplit la fiche du client
        clients[slot].socket_fd = client_fd;
        clients[slot].active    = 1;
        strncpy(clients[slot].ip, client_ip, INET_ADDRSTRLEN);
        pthread_mutex_unlock(&clients_mutex);

        // --- Lance un thread dédié à ce client ---
        // On passe l'adresse du slot comme argument au thread.
        // pthread_create() est non-bloquant : le thread démarre
        // en arrière-plan et on revient immédiatement à accept().
        if (pthread_create(&clients[slot].thread, NULL,
                           client_handler, &clients[slot]) != 0) {
            perror("[ERREUR] pthread_create()");
            pthread_mutex_lock(&clients_mutex);
            clients[slot].active = 0;
            pthread_mutex_unlock(&clients_mutex);
            close(client_fd);
        } else {
            // DETACH : le thread libère ses ressources tout seul
            // quand il termine (on n'a pas besoin de pthread_join)
            pthread_detach(clients[slot].thread);
        }
    }
}

/* =========================================================
 *  main()
 * ========================================================= */

int main(void) {
    // Crée le dossier de stockage s'il n'existe pas
    mkdir(STORAGE_DIR, 0755);

    // Initialise le tableau clients à zéro
    memset(clients, 0, sizeof(clients));

    // Intercepte Ctrl+C pour un arrêt propre
    signal(SIGINT, handle_sigint);

    // Initialise la socket serveur
    int server_fd = server_init(PORT);
    if (server_fd < 0) return EXIT_FAILURE;

    server_fd_global = server_fd;

    // Lance la boucle principale (ne retourne jamais)
    server_run(server_fd);

    return EXIT_SUCCESS;
}
