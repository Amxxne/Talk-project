import tkinter as tk
import os
import sys
from gui import SafeSyncGUI
from sync_client import SyncClient
from watcher import FolderWatcher

SERVER_HOST = "10.0.0.94"
SYNC_FOLDER = os.path.expanduser("~/SafeSync")

def main():
    # 1. GUI en premier
    root = tk.Tk()
    gui  = SafeSyncGUI(root, SYNC_FOLDER, SERVER_HOST)

    # 2. Client réseau APRÈS la GUI
    def start_backend():
        client = SyncClient(
            server_host=SERVER_HOST,
            sync_folder=SYNC_FOLDER,
            on_notify=lambda filename: gui.on_status(
                f"Notification : '{filename}' a changé", 'warning'
            ),
            on_status=lambda msg: gui.on_status(msg)
        )
        watcher = FolderWatcher(SYNC_FOLDER, client)
        watcher.start()
        gui.on_status("SafeSync démarré ✓", 'success')

        # Arrêt propre
        root.protocol("WM_DELETE_WINDOW", lambda: [watcher.stop(), root.destroy()])

    # Lance le backend 500ms après l'ouverture de la fenêtre
    root.after(500, start_backend)
    root.mainloop()

if __name__ == "__main__":
    main()
