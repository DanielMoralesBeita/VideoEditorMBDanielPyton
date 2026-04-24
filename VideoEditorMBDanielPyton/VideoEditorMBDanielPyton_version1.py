"""
Architect Video Editor
======================
Aplicación de escritorio para edición básica de video.
Permite cargar un video, recortar los primeros 10 segundos
y extraer el audio como MP3.

Dependencias:
    pip install customtkinter moviepy Pillow
"""

import os
import sys
import subprocess
import threading

import customtkinter as ctk
from tkinter import filedialog, messagebox
from moviepy.editor import VideoFileClip
from PIL import Image


# =============================================================================
# MOTOR DE PROCESAMIENTO (BACKEND)
# Toda la lógica de video vive aquí, desacoplada de la UI.
# Cada método corre dentro de un hilo secundario; cuando termina
# invoca `callback(success: bool, message: str)` para notificar a la UI.
# =============================================================================

class VideoEngine:

    @staticmethod
    def cut_logic(input_p: str, output_p: str, start: float, end: float, callback):
        """
        Recorta el video entre `start` y `end` segundos.

        Args:
            input_p:  Ruta del video de entrada.
            output_p: Ruta donde se guardará el clip recortado.
            start:    Segundo de inicio del corte.
            end:      Segundo de fin del corte (se limita a la duración real).
            callback: Función (success, message) que se llama al terminar.
        """
        try:
            with VideoFileClip(input_p) as video:
                # Aseguramos no pedir más segundos de los que tiene el video
                safe_end = min(end, video.duration)
                new_video = video.subclip(start, safe_end)
                new_video.write_videofile(output_p, codec="libx264", audio_codec="aac")
            callback(True, f"Guardado en: {output_p}")
        except Exception as e:
            callback(False, str(e))

    @staticmethod
    def extract_audio_logic(input_p: str, output_p: str, callback):
        """
        Extrae la pista de audio del video y la guarda como archivo de audio.

        Args:
            input_p:  Ruta del video de entrada.
            output_p: Ruta donde se guardará el audio extraído.
            callback: Función (success, message) que se llama al terminar.
        """
        try:
            with VideoFileClip(input_p) as video:
                # Verificamos que el video tenga pista de audio antes de continuar
                if video.audio is None:
                    callback(False, "El video no tiene pista de audio.")
                    return
                video.audio.write_audiofile(output_p)
            callback(True, f"Audio extraído: {output_p}")
        except Exception as e:
            callback(False, str(e))

    @staticmethod
    def load_thumbnail_logic(input_p: str, callback):
        """
        Genera una miniatura del video en segundo plano para no bloquear la UI.

        Args:
            input_p:  Ruta del video.
            callback: Función (success, PIL.Image | message) que se llama al terminar.
        """
        try:
            with VideoFileClip(input_p) as clip:
                # Tomamos el fotograma del segundo 1 (evita frames negros iniciales)
                frame = clip.get_frame(min(1, clip.duration - 0.01))
                img = Image.fromarray(frame).resize((640, 360))
            callback(True, img)
        except Exception as e:
            callback(False, str(e))


# =============================================================================
# INTERFAZ DE USUARIO (FRONTEND)
# Extiende CTk para aprovechar el tema moderno de customtkinter.
# Toda actualización de widgets desde hilos secundarios pasa por self.after()
# para garantizar thread-safety con Tkinter.
# =============================================================================

class ArchitectVideoApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Architect Video Editor")
        self.geometry("900x620")

        # Motor de procesamiento
        self.engine = VideoEngine()

        # Ruta del video actualmente cargado (None si no hay ninguno)
        self.selected_path: str | None = None

        # Referencia a la imagen de preview (debe mantenerse en self para evitar
        # que el garbage collector la elimine y deje el label en blanco)
        self._preview_image = None

        self._build_layout()

    # -------------------------------------------------------------------------
    # Construcción de la interfaz
    # -------------------------------------------------------------------------

    def _build_layout(self):
        """Crea y posiciona todos los widgets de la ventana."""

        # Columna 1 (sidebar) fija; columna 2 (preview) expansible
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar izquierdo ---
        self.sidebar = ctk.CTkFrame(self, width=210)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.sidebar.grid_propagate(False)  # Mantiene el ancho fijo

        ctk.CTkLabel(
            self.sidebar,
            text="Architect Video Editor",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(pady=(20, 5), padx=10)

        # Etiqueta que muestra el nombre del archivo cargado
        self.lbl_filename = ctk.CTkLabel(
            self.sidebar,
            text="Sin video cargado",
            font=ctk.CTkFont(size=11),
            wraplength=180,
            text_color="gray"
        )
        self.lbl_filename.pack(pady=(0, 15), padx=10)

        self.btn_load = ctk.CTkButton(
            self.sidebar,
            text="📁  Cargar Video",
            command=self.load_video
        )
        self.btn_load.pack(pady=8, padx=10, fill="x")

        self.btn_cut = ctk.CTkButton(
            self.sidebar,
            text="✂  Cortar primeros 10s",
            command=self.run_cut,
            state="disabled"     # Se habilita solo cuando hay video cargado
        )
        self.btn_cut.pack(pady=8, padx=10, fill="x")

        self.btn_audio = ctk.CTkButton(
            self.sidebar,
            text="🎙  Extraer Audio",
            command=self.run_audio,
            state="disabled"
        )
        self.btn_audio.pack(pady=8, padx=10, fill="x")

        # --- Área de preview (derecha) ---
        self.preview_frame = ctk.CTkFrame(self, fg_color="black")
        self.preview_frame.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="nsew")

        self.img_label = ctk.CTkLabel(
            self.preview_frame,
            text="Carga un video para ver la vista previa",
            text_color="gray"
        )
        self.img_label.pack(expand=True)

        # --- Barra de progreso (fila inferior, ancho completo) ---
        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="ew")
        self.progress.set(0)

        # Etiqueta de estado debajo de la barra de progreso
        self.lbl_status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11))
        self.lbl_status.grid(row=2, column=0, columnspan=2, pady=(0, 8))

    # -------------------------------------------------------------------------
    # Acciones del usuario
    # -------------------------------------------------------------------------

    def load_video(self):
        """Abre un diálogo para seleccionar un video y genera la miniatura en segundo plano."""
        path = filedialog.askopenfilename(
            title="Seleccionar video",
            filetypes=[("Archivos de video", "*.mp4 *.avi *.mkv *.mov *.webm")]
        )
        if not path:
            return  # El usuario canceló el diálogo

        self.selected_path = path
        filename = os.path.basename(path)
        self.lbl_filename.configure(text=filename, text_color="white")
        self.lbl_status.configure(text="Cargando miniatura…")

        # Generamos la miniatura en un hilo para no bloquear la UI
        threading.Thread(
            target=self.engine.load_thumbnail_logic,
            args=(path, self._on_thumbnail_loaded),
            daemon=True
        ).start()

    def run_cut(self):
        """Solicita una ruta de salida y lanza el corte de video en un hilo."""
        out = filedialog.asksaveasfilename(
            title="Guardar clip recortado",
            defaultextension=".mp4",
            filetypes=[("Video MP4", "*.mp4")]
        )
        if not out:
            return  # El usuario canceló el diálogo

        self._set_processing_state(True)
        threading.Thread(
            target=self.engine.cut_logic,
            args=(self.selected_path, out, 0, 10, self._on_task_finished),
            daemon=True
        ).start()

    def run_audio(self):
        """Solicita una ruta de salida y lanza la extracción de audio en un hilo."""
        out = filedialog.asksaveasfilename(
            title="Guardar audio extraído",
            defaultextension=".mp3",
            filetypes=[("Audio MP3", "*.mp3")]
        )
        if not out:
            return  # El usuario canceló el diálogo

        self._set_processing_state(True)
        threading.Thread(
            target=self.engine.extract_audio_logic,
            args=(self.selected_path, out, self._on_task_finished),
            daemon=True
        ).start()

    # -------------------------------------------------------------------------
    # Callbacks (siempre redirigidos al hilo principal con self.after)
    # -------------------------------------------------------------------------

    def _on_thumbnail_loaded(self, success: bool, result):
        """
        Callback del hilo de miniatura.
        Redirige la actualización de UI al hilo principal de Tkinter.
        """
        self.after(0, self._update_preview, success, result)

    def _update_preview(self, success: bool, result):
        """Actualiza el widget de preview con la miniatura generada (hilo principal)."""
        if success:
            # Guardamos la referencia en self para que el GC no la elimine
            self._preview_image = ctk.CTkImage(result, size=(640, 360))
            self.img_label.configure(image=self._preview_image, text="")
            self.lbl_status.configure(text="Video cargado correctamente ✓")
            # Habilitamos los botones de acción
            self.btn_cut.configure(state="normal")
            self.btn_audio.configure(state="normal")
        else:
            self.lbl_status.configure(text=f"Error al cargar miniatura: {result}")

    def _on_task_finished(self, success: bool, message: str):
        """
        Callback genérico para corte y extracción de audio.
        Redirige la actualización de UI al hilo principal de Tkinter.
        """
        self.after(0, self._finish_task_ui, success, message)

    def _finish_task_ui(self, success: bool, message: str):
        """Actualiza la UI tras finalizar una tarea de procesamiento (hilo principal)."""
        self._set_processing_state(False)

        if success:
            self.progress.set(1)
            self.lbl_status.configure(text="Completado ✓")
            messagebox.showinfo("¡Listo!", message)
            # Abrimos la carpeta de destino de forma multiplataforma
            self._open_folder(os.path.dirname(self.selected_path))
        else:
            self.progress.set(0)
            self.lbl_status.configure(text="Error en el procesamiento ✗")
            messagebox.showerror("Error", message)

    # -------------------------------------------------------------------------
    # Utilidades internas
    # -------------------------------------------------------------------------

    def _set_processing_state(self, processing: bool):
        """
        Habilita o deshabilita los botones de acción y la barra de progreso
        según si hay una tarea en curso.

        Args:
            processing: True para bloquear la UI, False para liberarla.
        """
        state = "disabled" if processing else "normal"
        self.btn_load.configure(state=state)
        self.btn_cut.configure(state=state)
        self.btn_audio.configure(state=state)

        if processing:
            self.progress.set(0)
            self.progress.start()   # Animación indeterminada
            self.lbl_status.configure(text="Procesando…")
        else:
            self.progress.stop()

    @staticmethod
    def _open_folder(folder_path: str):
        """
        Abre el explorador de archivos en la carpeta indicada.
        Funciona en Windows, macOS y Linux.

        Args:
            folder_path: Ruta absoluta de la carpeta a abrir.
        """
        if not folder_path or not os.path.isdir(folder_path):
            return

        if sys.platform == "win32":
            os.startfile(folder_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder_path])
        else:
            # Linux y otros sistemas tipo Unix
            subprocess.Popen(["xdg-open", folder_path])


# =============================================================================
# Punto de entrada
# =============================================================================

if __name__ == "__main__":
    # Tema oscuro por defecto; se puede cambiar a "light" o "system"
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    app = ArchitectVideoApp()
    app.mainloop()