"""
gui.py — Interface graphique SafeSync (Tkinter)

Architecture de la GUI :
  - Un thread principal Tkinter (obligatoire, Tkinter n'est pas thread-safe)
  - Les autres threads (sender, listener, watcher) communiquent via
    une Queue → le thread Tkinter la poll toutes les 100ms avec after()

Règle d'or Tkinter : on ne JAMAIS modifier un widget depuis un autre thread.
On passe par la queue, et Tkinter met à jour lui-même.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import queue
import os
import time
from datetime import datetime


# ─────────────────────────────────────────────────────────────
#  PALETTE DE COULEURS
# ─────────────────────────────────────────────────────────────

COLORS = {
    'bg':           '#1E1E2E',   # fond principal (gris foncé bleuté)
    'panel':        '#2A2A3E',   # fond des panneaux
    'accent':       '#7C3AED',   # violet principal
    'accent_light': '#A855F7',   # violet clair (hover)
    'success':      '#10B981',   # vert (connecté / succès)
    'warning':      '#F59E0B',   # orange (en attente)
    'error':        '#EF4444',   # rouge (erreur / hors ligne)
    'text':         '#E2E8F0',   # texte principal
    'text_muted':   '#94A3B8',   # texte secondaire
    'border':       '#3F3F5A',   # bordures
}


class SafeSyncGUI:
    """
    Fenêtre principale de SafeSync.

    Reçoit les mises à jour via une Queue thread-safe,
    et les applique dans la boucle Tkinter principale.
    """

    def __init__(self, root, sync_folder, server_host):
        self.root         = root
        self.sync_folder  = sync_folder
        self.server_host  = server_host

        # Queue de communication inter-threads → GUI
        # Les autres threads font : gui_queue.put(('type', data))
        # La GUI consomme : self._process_queue() toutes les 100ms
        self.gui_queue = queue.Queue()

        # Historique des transferts (max 50 entrées)
        self.transfer_history = []
        self.MAX_HISTORY      = 50

        self._setup_window()
        self._build_ui()

        # Lance la boucle de polling de la queue
        self._poll_queue()

    # ─────────────────────────────────────────────────────────
    #  CONFIGURATION FENÊTRE
    # ─────────────────────────────────────────────────────────

    def _setup_window(self):
        self.root.title("SafeSync")
        self.root.geometry("520x620")
        self.root.resizable(False, False)
        self.root.configure(bg=COLORS['bg'])

        # Icône (si disponible)
        try:
            self.root.iconbitmap('icon.ico')
        except:
            pass

        # Fermeture propre
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────────────────────
    #  CONSTRUCTION DE L'UI
    # ─────────────────────────────────────────────────────────

    def _build_ui(self):
        """Construit tous les widgets de l'interface."""
        # Padding général
        main = tk.Frame(self.root, bg=COLORS['bg'], padx=20, pady=20)
        main.pack(fill='both', expand=True)

        self._build_header(main)
        self._build_status_panel(main)
        self._build_storage_panel(main)
        self._build_history_panel(main)
        self._build_footer(main)

    def _build_header(self, parent):
        """Logo + titre."""
        frame = tk.Frame(parent, bg=COLORS['bg'])
        frame.pack(fill='x', pady=(0, 20))

        # Logo textuel stylisé
        logo = tk.Label(
            frame,
            text="⬡ SafeSync",
            font=('Consolas', 22, 'bold'),
            fg=COLORS['accent_light'],
            bg=COLORS['bg']
        )
        logo.pack(side='left')

        # Sous-titre
        subtitle = tk.Label(
            frame,
            text="On-Premise File Sync",
            font=('Consolas', 10),
            fg=COLORS['text_muted'],
            bg=COLORS['bg']
        )
        subtitle.pack(side='left', padx=(10, 0), pady=(8, 0))

    def _build_status_panel(self, parent):
        """Panneau : état de connexion + serveur."""
        panel = tk.Frame(
            parent,
            bg=COLORS['panel'],
            relief='flat',
            bd=0,
            padx=16, pady=14
        )
        panel.pack(fill='x', pady=(0, 12))

        # Ligne 1 : indicateur de statut
        row1 = tk.Frame(panel, bg=COLORS['panel'])
        row1.pack(fill='x')

        # Cercle de statut (LED)
        self.status_led = tk.Label(
            row1,
            text="●",
            font=('Arial', 14),
            fg=COLORS['error'],   # Rouge par défaut (déconnecté)
            bg=COLORS['panel']
        )
        self.status_led.pack(side='left')

        self.status_label = tk.Label(
            row1,
            text="Hors ligne",
            font=('Consolas', 12, 'bold'),
            fg=COLORS['error'],
            bg=COLORS['panel']
        )
        self.status_label.pack(side='left', padx=(6, 0))

        # Ligne 2 : adresse serveur
        row2 = tk.Frame(panel, bg=COLORS['panel'])
        row2.pack(fill='x', pady=(6, 0))

        tk.Label(
            row2,
            text=f"Serveur : {self.server_host}:5000",
            font=('Consolas', 10),
            fg=COLORS['text_muted'],
            bg=COLORS['panel']
        ).pack(side='left')

        # Ligne 3 : dossier synchronisé
        row3 = tk.Frame(panel, bg=COLORS['panel'])
        row3.pack(fill='x', pady=(4, 0))

        folder_short = self.sync_folder
        if len(folder_short) > 40:
            folder_short = '...' + folder_short[-37:]

        tk.Label(
            row3,
            text=f"📁 {folder_short}",
            font=('Consolas', 10),
            fg=COLORS['text_muted'],
            bg=COLORS['panel']
        ).pack(side='left')

    def _build_storage_panel(self, parent):
        """Panneau : jauge de stockage local."""
        panel = tk.Frame(
            parent,
            bg=COLORS['panel'],
            padx=16, pady=14
        )
        panel.pack(fill='x', pady=(0, 12))

        # Titre du panneau
        title_row = tk.Frame(panel, bg=COLORS['panel'])
        title_row.pack(fill='x')

        tk.Label(
            title_row,
            text="Stockage synchronisé",
            font=('Consolas', 11, 'bold'),
            fg=COLORS['text'],
            bg=COLORS['panel']
        ).pack(side='left')

        self.storage_label = tk.Label(
            title_row,
            text="0 Ko / calcul...",
            font=('Consolas', 10),
            fg=COLORS['text_muted'],
            bg=COLORS['panel']
        )
        self.storage_label.pack(side='right')

        # Barre de progression (ttk.Progressbar)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure(
            "SafeSync.Horizontal.TProgressbar",
            troughcolor=COLORS['border'],
            background=COLORS['accent'],
            bordercolor=COLORS['panel'],
            lightcolor=COLORS['accent'],
            darkcolor=COLORS['accent'],
        )

        self.storage_bar = ttk.Progressbar(
            panel,
            style="SafeSync.Horizontal.TProgressbar",
            orient='horizontal',
            length=460,
            mode='determinate',
            maximum=100
        )
        self.storage_bar.pack(fill='x', pady=(8, 0))

        self.storage_bar['value'] = 0

        # Mise à jour initiale
        self._update_storage()

    def _build_history_panel(self, parent):
        """Panneau : historique des transferts récents."""
        panel = tk.Frame(parent, bg=COLORS['panel'], padx=16, pady=14)
        panel.pack(fill='both', expand=True, pady=(0, 12))

        # En-tête
        header = tk.Frame(panel, bg=COLORS['panel'])
        header.pack(fill='x', pady=(0, 8))

        tk.Label(
            header,
            text="Activité récente",
            font=('Consolas', 11, 'bold'),
            fg=COLORS['text'],
            bg=COLORS['panel']
        ).pack(side='left')

        # Bouton "Effacer"
        clear_btn = tk.Label(
            header,
            text="Effacer",
            font=('Consolas', 9),
            fg=COLORS['text_muted'],
            bg=COLORS['panel'],
            cursor='hand2'
        )
        clear_btn.pack(side='right')
        clear_btn.bind('<Button-1>', lambda e: self._clear_history())

        # Zone de texte scrollable
        self.history_text = scrolledtext.ScrolledText(
            panel,
            height=10,
            font=('Consolas', 10),
            bg='#16162A',
            fg=COLORS['text'],
            insertbackground=COLORS['text'],
            relief='flat',
            state='disabled',        # Lecture seule
            wrap='word',
            padx=8, pady=8
        )
        self.history_text.pack(fill='both', expand=True)

        # Tags de couleur pour les différents types de messages
        self.history_text.tag_config('success', foreground=COLORS['success'])
        self.history_text.tag_config('warning', foreground=COLORS['warning'])
        self.history_text.tag_config('error',   foreground=COLORS['error'])
        self.history_text.tag_config('info',    foreground=COLORS['text_muted'])
        self.history_text.tag_config('time',    foreground=COLORS['border'])

    def _build_footer(self, parent):
        """Barre du bas : fichiers en attente."""
        self.footer_label = tk.Label(
            parent,
            text="En attente : 0 fichier(s)",
            font=('Consolas', 9),
            fg=COLORS['text_muted'],
            bg=COLORS['bg']
        )
        self.footer_label.pack(anchor='w')

    # ─────────────────────────────────────────────────────────
    #  MISE À JOUR DE LA GUI
    #
    #  Ces méthodes sont appelées UNIQUEMENT depuis le thread
    #  Tkinter (via _poll_queue). Jamais directement depuis
    #  un autre thread.
    # ─────────────────────────────────────────────────────────

    def set_connected(self, connected: bool):
        """Met à jour l'indicateur de connexion."""
        if connected:
            color = COLORS['success']
            text  = "En ligne"
        else:
            color = COLORS['error']
            text  = "Hors ligne"

        self.status_led.config(fg=color)
        self.status_label.config(text=text, fg=color)

    def add_history_entry(self, message: str, level: str = 'info'):
        """
        Ajoute une ligne dans l'historique.
        level : 'success', 'warning', 'error', 'info'
        """
        timestamp = datetime.now().strftime('%H:%M:%S')

        # On active brièvement l'édition pour insérer
        self.history_text.config(state='normal')

        # Insère l'horodatage en gris
        self.history_text.insert('end', f"[{timestamp}] ", 'time')

        # Insère le message avec la couleur du niveau
        self.history_text.insert('end', f"{message}\n", level)

        # Auto-scroll vers le bas
        self.history_text.see('end')

        # Remet en lecture seule
        self.history_text.config(state='disabled')

        # Limite l'historique
        self.transfer_history.append((timestamp, message, level))
        if len(self.transfer_history) > self.MAX_HISTORY:
            self.transfer_history.pop(0)

    def _clear_history(self):
        self.history_text.config(state='normal')
        self.history_text.delete('1.0', 'end')
        self.history_text.config(state='disabled')
        self.transfer_history.clear()

    def update_queue_count(self, count: int):
        """Met à jour le compteur de fichiers en attente."""
        if count == 0:
            text = "En attente : 0 fichier(s)"
        elif count == 1:
            text = "⏳ En attente : 1 fichier"
        else:
            text = f"⏳ En attente : {count} fichiers"
        self.footer_label.config(text=text)

    def _update_storage(self):
        """Calcule et affiche l'espace utilisé dans le dossier sync."""
        try:
            total_size = 0
            file_count = 0

            if os.path.exists(self.sync_folder):
                for f in os.listdir(self.sync_folder):
                    fp = os.path.join(self.sync_folder, f)
                    if os.path.isfile(fp):
                        total_size += os.path.getsize(fp)
                        file_count += 1

            # Formate la taille lisiblement
            if total_size < 1024:
                size_str = f"{total_size} o"
            elif total_size < 1024 ** 2:
                size_str = f"{total_size / 1024:.1f} Ko"
            elif total_size < 1024 ** 3:
                size_str = f"{total_size / (1024**2):.1f} Mo"
            else:
                size_str = f"{total_size / (1024**3):.2f} Go"

            # Limite arbitraire pour la jauge : 1 Go
            MAX_DISPLAY = 1024 ** 3
            percent = min(100, (total_size / MAX_DISPLAY) * 100)

            self.storage_label.config(
                text=f"{size_str} — {file_count} fichier(s)"
            )
            self.storage_bar['value'] = percent

        except Exception as e:
            self.storage_label.config(text="Erreur lecture dossier")

        # Replanifie la mise à jour toutes les 5 secondes
        self.root.after(5000, self._update_storage)

    # ─────────────────────────────────────────────────────────
    #  QUEUE INTER-THREADS
    #
    #  C'est le mécanisme central pour la thread-safety de Tkinter.
    #  Les autres threads ne touchent JAMAIS les widgets directement.
    #  Ils mettent un message dans la queue, et Tkinter traite.
    # ─────────────────────────────────────────────────────────

    def _poll_queue(self):
        """
        Appelée toutes les 100ms par Tkinter.
        Vide la queue de messages et met à jour les widgets.
        """
        try:
            # Traite jusqu'à 10 messages par cycle pour éviter la latence
            for _ in range(10):
                msg_type, data = self.gui_queue.get_nowait()

                if msg_type == 'status':
                    self.add_history_entry(data['message'], data.get('level', 'info'))

                elif msg_type == 'connected':
                    self.set_connected(data)
                    label = "En ligne" if data else "Hors ligne"
                    level = "success" if data else "error"
                    self.add_history_entry(label, level)

                elif msg_type == 'queue_count':
                    self.update_queue_count(data)

                elif msg_type == 'storage_refresh':
                    self._update_storage()

        except queue.Empty:
            pass  # Normal, la queue est vide

        # Replanifie dans 100ms (boucle non bloquante)
        self.root.after(100, self._poll_queue)

    # ─────────────────────────────────────────────────────────
    #  CALLBACKS PUBLICS (appelés depuis d'autres threads)
    #
    #  Ces méthodes sont thread-safe car elles ne font que
    #  mettre un message dans la queue. Elles ne touchent
    #  jamais un widget directement.
    # ─────────────────────────────────────────────────────────

    def on_status(self, message: str, level: str = 'info'):
        """Callback de statut — appelable depuis n'importe quel thread."""
        self.gui_queue.put(('status', {'message': message, 'level': level}))

    def on_connected(self, connected: bool):
        """Notifie la GUI d'un changement de connexion."""
        self.gui_queue.put(('connected', connected))

    def on_queue_change(self, count: int):
        """Notifie la GUI du nombre de fichiers en attente."""
        self.gui_queue.put(('queue_count', count))

    # ─────────────────────────────────────────────────────────
    #  FERMETURE
    # ─────────────────────────────────────────────────────────

    def _on_close(self):
        """Fermeture propre de la fenêtre."""
        self.root.destroy()

    def run(self):
        """Lance la boucle principale Tkinter (bloquante)."""
        self.root.mainloop()
