"""
Architect Video Editor
======================
Aplicación de escritorio para edición básica de video con reproductor integrado.

Características:
  - Reproducción frame a frame con Play / Pause / Stop
  - Scrubbing: arrastrar la barra de tiempo para saltar a cualquier punto
  - Marcadores IN / OUT ajustables para definir el rango de corte
  - Corte preciso entre los marcadores IN y OUT
  - Extracción de audio como MP3
  - Abre la carpeta de salida al terminar (Windows / macOS / Linux)

Dependencias:
    pip install customtkinter moviepy Pillow

Nota sobre audio durante la reproducción:
    El reproductor solo muestra video (frames renderizados en Tkinter).
    Tkinter no tiene soporte nativo de audio; para audio en tiempo real
    se necesitaría pygame o vlc, lo cual está fuera del alcance de este módulo.
"""

import os
import sys
import subprocess
import threading
import time

import customtkinter as ctk
from tkinter import filedialog, messagebox
from moviepy.editor import VideoFileClip
from PIL import Image


# =============================================================================
# MOTOR DE PROCESAMIENTO (BACKEND)
# Toda la lógica de video vive aquí, desacoplada de la UI.
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
                safe_start = max(0.0, start)
                safe_end   = min(end, video.duration)
                if safe_start >= safe_end:
                    callback(False, "El rango IN–OUT es inválido (IN >= OUT).")
                    return
                clip = video.subclip(safe_start, safe_end)
                clip.write_videofile(output_p, codec="libx264", audio_codec="aac")
            callback(True, f"Guardado en: {output_p}")
        except Exception as e:
            callback(False, str(e))

    @staticmethod
    def extract_audio_logic(input_p: str, output_p: str, callback):
        """
        Extrae la pista de audio del video y la guarda como MP3.

        Args:
            input_p:  Ruta del video de entrada.
            output_p: Ruta de salida del audio.
            callback: Función (success, message) que se llama al terminar.
        """
        try:
            with VideoFileClip(input_p) as video:
                if video.audio is None:
                    callback(False, "El video no tiene pista de audio.")
                    return
                video.audio.write_audiofile(output_p)
            callback(True, f"Audio extraído: {output_p}")
        except Exception as e:
            callback(False, str(e))

    @staticmethod
    def load_video_info(input_p: str, callback):
        """
        Carga metadatos y primer frame del video en segundo plano.

        Llama a callback(True, {"duration": float, "fps": float, "thumb": PIL.Image})
        o callback(False, mensaje_error).
        """
        try:
            with VideoFileClip(input_p) as clip:
                duration = clip.duration
                fps      = clip.fps
                # Primer frame visible (evitamos frames negros del inicio)
                t     = min(0.5, duration - 0.01)
                frame = clip.get_frame(t)
                thumb = Image.fromarray(frame)
            callback(True, {"duration": duration, "fps": fps, "thumb": thumb})
        except Exception as e:
            callback(False, str(e))

    @staticmethod
    def get_frame(input_p: str, t: float, callback):
        """
        Extrae un único frame en el instante `t` segundos.
        Se usa durante el scrubbing (seek manual).

        Args:
            input_p:  Ruta del video.
            t:        Instante en segundos.
            callback: Función (success, PIL.Image | error_str).
        """
        try:
            with VideoFileClip(input_p) as clip:
                t_safe = max(0.0, min(t, clip.duration - 0.01))
                frame  = clip.get_frame(t_safe)
                img    = Image.fromarray(frame)
            callback(True, img)
        except Exception as e:
            callback(False, str(e))


# =============================================================================
# REPRODUCTOR DE VIDEO
# Corre en un hilo daemon. Avanza fotograma a fotograma a la velocidad
# del FPS original y notifica a la UI mediante callbacks.
# =============================================================================

class VideoPlayer:
    """
    Controla el estado de reproducción (play / pause / stop / seek).
    """

    def __init__(self, video_path: str, fps: float, duration: float,
                 frame_callback, position_callback):
        """
        Args:
            video_path:        Ruta del video.
            fps:               Fotogramas por segundo del video.
            duration:          Duración total en segundos.
            frame_callback:    fn(PIL.Image) llamada con cada nuevo frame.
            position_callback: fn(float) llamada con la posición actual (segundos).
        """
        self.video_path        = video_path
        self.fps               = max(fps, 1.0)      # Evita división por cero
        self.duration          = duration
        self.frame_callback    = frame_callback
        self.position_callback = position_callback

        self._position  = 0.0     # Posición actual en segundos
        self._playing   = False   # True mientras reproduce
        self._stop_flag = False   # True para terminar el hilo
        self._lock      = threading.Lock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Controles públicos (thread-safe)
    # ------------------------------------------------------------------

    def play(self):
        """Inicia o reanuda la reproducción."""
        with self._lock:
            if self._playing:
                return
            self._playing   = True
            self._stop_flag = False

        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def pause(self):
        """Pausa sin resetear la posición."""
        with self._lock:
            self._playing = False

    def stop(self):
        """Detiene y regresa al inicio."""
        with self._lock:
            self._playing   = False
            self._stop_flag = True
            self._position  = 0.0

    def seek(self, t: float):
        """Salta a la posición `t` segundos (funciona en pausa y reproducción)."""
        with self._lock:
            self._position = max(0.0, min(t, self.duration))

    @property
    def position(self) -> float:
        """Posición actual en segundos (thread-safe)."""
        with self._lock:
            return self._position

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    # ------------------------------------------------------------------
    # Bucle interno de reproducción
    # ------------------------------------------------------------------

    def _run(self):
        """
        Bucle principal: extrae frames con VideoFileClip y respeta el FPS.
        Termina cuando llega al final, se pausa o se llama stop().
        """
        frame_duration = 1.0 / self.fps   # Tiempo entre frames en segundos

        with VideoFileClip(self.video_path) as clip:
            while True:
                # ---- Leer estado con lock ----
                with self._lock:
                    if self._stop_flag:
                        break
                    if not self._playing:
                        time.sleep(0.05)   # Espera liviana mientras está pausado
                        continue
                    t = self._position

                # ---- Verificar fin del video ----
                if t >= self.duration:
                    with self._lock:
                        self._playing  = False
                        self._position = 0.0
                    self.position_callback(0.0)
                    break

                # ---- Obtener y enviar frame ----
                t_safe = min(t, clip.duration - 0.01)
                try:
                    raw   = clip.get_frame(t_safe)
                    frame = Image.fromarray(raw)
                    self.frame_callback(frame)
                    self.position_callback(t)
                except Exception:
                    pass   # Frame corrupto: lo saltamos silenciosamente

                # ---- Avanzar posición y respetar FPS ----
                with self._lock:
                    self._position = t + frame_duration
                time.sleep(frame_duration)


# =============================================================================
# INTERFAZ DE USUARIO (FRONTEND)
# =============================================================================

class ArchitectVideoApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Architect Video Editor")
        self.geometry("1060x730")
        self.minsize(900, 640)

        # -- Estado de la aplicación --
        self.engine         = VideoEngine()
        self.selected_path  = None     # Ruta del video cargado
        self.video_duration = 0.0      # Duración total en segundos
        self.video_fps      = 25.0     # FPS del video

        # Marcadores IN / OUT en segundos
        self.mark_in  = 0.0
        self.mark_out = 0.0

        # Instancia del reproductor (se crea cuando se carga un video)
        self.player: VideoPlayer | None = None

        # Referencia a la imagen de preview (evita garbage collection)
        self._preview_image = None

        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # =========================================================================
    # Construcción de la interfaz
    # =========================================================================

    def _build_layout(self):
        """Crea y posiciona todos los widgets de la ventana."""
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main_area()

    # -------------------------------------------------------------------------
    # Sidebar izquierdo
    # -------------------------------------------------------------------------

    def _build_sidebar(self):
        """Panel con botones de acción, marcadores IN/OUT y exportación."""

        self.sidebar = ctk.CTkFrame(self, width=225)
        self.sidebar.grid(row=0, column=0, rowspan=4, sticky="nsew",
                          padx=10, pady=10)
        self.sidebar.grid_propagate(False)

        # Título
        ctk.CTkLabel(
            self.sidebar,
            text="Architect\nVideo Editor",
            font=ctk.CTkFont(size=15, weight="bold"),
            justify="center"
        ).pack(pady=(20, 4), padx=10)

        # Nombre del archivo actualmente cargado
        self.lbl_filename = ctk.CTkLabel(
            self.sidebar,
            text="Sin video cargado",
            font=ctk.CTkFont(size=11),
            wraplength=200,
            text_color="gray"
        )
        self.lbl_filename.pack(pady=(0, 14), padx=10)

        # ---- Cargar archivo ----
        self.btn_load = ctk.CTkButton(
            self.sidebar, text="📁  Cargar Video", command=self.load_video
        )
        self.btn_load.pack(pady=6, padx=12, fill="x")

        # ---- Sección Marcadores ----
        ctk.CTkLabel(
            self.sidebar, text="─── Marcadores ───",
            font=ctk.CTkFont(size=11), text_color="gray"
        ).pack(pady=(16, 4))

        # Fila marcador IN
        frm_in = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        frm_in.pack(fill="x", padx=12, pady=3)

        self.btn_set_in = ctk.CTkButton(
            frm_in, text="[ IN", width=64,
            fg_color="#2d6a2d", hover_color="#3a8a3a",
            command=self.set_mark_in, state="disabled"
        )
        self.btn_set_in.pack(side="left")

        self.lbl_in = ctk.CTkLabel(
            frm_in, text="0.00 s",
            font=ctk.CTkFont(size=12, family="Courier"), width=85
        )
        self.lbl_in.pack(side="left", padx=(8, 0))

        # Fila marcador OUT
        frm_out = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        frm_out.pack(fill="x", padx=12, pady=3)

        self.btn_set_out = ctk.CTkButton(
            frm_out, text="OUT ]", width=64,
            fg_color="#8b2020", hover_color="#b02828",
            command=self.set_mark_out, state="disabled"
        )
        self.btn_set_out.pack(side="left")

        self.lbl_out = ctk.CTkLabel(
            frm_out, text="0.00 s",
            font=ctk.CTkFont(size=12, family="Courier"), width=85
        )
        self.lbl_out.pack(side="left", padx=(8, 0))

        # Duración del rango seleccionado
        self.lbl_range = ctk.CTkLabel(
            self.sidebar, text="Rango: 0.00 s",
            font=ctk.CTkFont(size=11), text_color="#aaaaaa"
        )
        self.lbl_range.pack(pady=(4, 0))

        # ---- Sección Exportar ----
        ctk.CTkLabel(
            self.sidebar, text="─── Exportar ───",
            font=ctk.CTkFont(size=11), text_color="gray"
        ).pack(pady=(16, 4))

        self.btn_cut = ctk.CTkButton(
            self.sidebar, text="✂  Cortar IN → OUT",
            command=self.run_cut, state="disabled"
        )
        self.btn_cut.pack(pady=6, padx=12, fill="x")

        self.btn_audio = ctk.CTkButton(
            self.sidebar, text="🎙  Extraer Audio",
            command=self.run_audio, state="disabled"
        )
        self.btn_audio.pack(pady=6, padx=12, fill="x")

        # Info de duración total
        self.lbl_duration = ctk.CTkLabel(
            self.sidebar, text="",
            font=ctk.CTkFont(size=11), text_color="gray"
        )
        self.lbl_duration.pack(pady=(18, 0))

    # -------------------------------------------------------------------------
    # Área principal: preview + timeline + controles
    # -------------------------------------------------------------------------

    def _build_main_area(self):
        """Panel derecho: canvas de video, scrubber, barra de marcadores y controles."""

        main = ctk.CTkFrame(self)
        main.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)

        # ---- Preview de video ----
        self.preview_frame = ctk.CTkFrame(main, fg_color="#0d0d0d")
        self.preview_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        self.preview_frame.grid_rowconfigure(0, weight=1)
        self.preview_frame.grid_columnconfigure(0, weight=1)

        self.img_label = ctk.CTkLabel(
            self.preview_frame,
            text="Carga un video para comenzar",
            text_color="#555555",
            font=ctk.CTkFont(size=14)
        )
        self.img_label.grid(row=0, column=0, sticky="nsew")

        # ---- Barra visual de marcadores IN/OUT ----
        # Muestra el rango seleccionado coloreado sobre la línea de tiempo
        self.canvas_markers = ctk.CTkCanvas(
            main, height=14, bg="#1a1a1a", highlightthickness=0
        )
        self.canvas_markers.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 0))
        # Redibujar cuando el canvas cambie de tamaño
        self.canvas_markers.bind("<Configure>", lambda e: self._draw_markers())

        # ---- Scrubber (slider de posición) ----
        timeline_frame = ctk.CTkFrame(main, fg_color="transparent")
        timeline_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 0))
        timeline_frame.grid_columnconfigure(1, weight=1)

        # Tiempo actual
        self.lbl_current = ctk.CTkLabel(
            timeline_frame, text="0:00.0", width=58,
            font=ctk.CTkFont(size=11, family="Courier")
        )
        self.lbl_current.grid(row=0, column=0, padx=(0, 6))

        # Slider principal de posición
        self.slider_pos = ctk.CTkSlider(
            timeline_frame,
            from_=0, to=1,
            command=self._on_scrub
        )
        self.slider_pos.set(0)
        self.slider_pos.configure(state="disabled")
        self.slider_pos.grid(row=0, column=1, sticky="ew")

        # Tiempo total
        self.lbl_total = ctk.CTkLabel(
            timeline_frame, text="0:00.0", width=58,
            font=ctk.CTkFont(size=11, family="Courier")
        )
        self.lbl_total.grid(row=0, column=2, padx=(6, 0))

        # ---- Controles de reproducción ----
        ctrl = ctk.CTkFrame(main, fg_color="transparent")
        ctrl.grid(row=3, column=0, pady=(8, 10))

        # Botón Stop
        self.btn_stop = ctk.CTkButton(
            ctrl, text="⏹", width=46,
            command=self.stop_video, state="disabled",
            font=ctk.CTkFont(size=16)
        )
        self.btn_stop.pack(side="left", padx=5)

        # Botón Play / Pause
        self.btn_play = ctk.CTkButton(
            ctrl, text="▶", width=60,
            command=self.toggle_play, state="disabled",
            font=ctk.CTkFont(size=16)
        )
        self.btn_play.pack(side="left", padx=5)

        # Aviso de que no hay audio en preview (Tkinter no lo soporta nativamente)
        ctk.CTkLabel(
            ctrl, text="🔇  Sin audio en preview",
            font=ctk.CTkFont(size=10), text_color="#666666"
        ).pack(side="left", padx=(14, 0))

        # ---- Barra de progreso de exportación ----
        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=1, column=1, padx=(0, 10), pady=(0, 4), sticky="ew")
        self.progress.set(0)

        # ---- Etiqueta de estado ----
        self.lbl_status = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=11)
        )
        self.lbl_status.grid(row=2, column=1, pady=(0, 8))

    # =========================================================================
    # Carga de video
    # =========================================================================

    def load_video(self):
        """Abre el diálogo de archivo y carga el video seleccionado."""
        path = filedialog.askopenfilename(
            title="Seleccionar video",
            filetypes=[("Archivos de video", "*.mp4 *.avi *.mkv *.mov *.webm")]
        )
        if not path:
            return

        # Detener cualquier reproducción anterior
        if self.player:
            self.player.stop()
            self.player = None

        self.selected_path = path
        self.lbl_filename.configure(
            text=os.path.basename(path), text_color="white"
        )
        self.lbl_status.configure(text="Cargando video…")
        self._set_action_buttons("disabled")

        # Cargar metadatos y primer frame en segundo plano (no bloquea la UI)
        threading.Thread(
            target=self.engine.load_video_info,
            args=(path, self._on_video_loaded),
            daemon=True
        ).start()

    def _on_video_loaded(self, success: bool, result):
        """Callback del hilo de carga → redirige al hilo principal."""
        self.after(0, self._apply_video_loaded, success, result)

    def _apply_video_loaded(self, success: bool, result):
        """Actualiza la UI con los metadatos y frame inicial del video (hilo principal)."""
        if not success:
            self.lbl_status.configure(text=f"Error: {result}")
            return

        self.video_duration = result["duration"]
        self.video_fps      = result["fps"]

        # Inicializar marcadores al rango completo del video
        self.mark_in  = 0.0
        self.mark_out = self.video_duration

        # Mostrar frame inicial en el preview
        self._show_frame(result["thumb"])

        # Configurar slider con el rango real del video
        self.slider_pos.configure(state="normal", to=self.video_duration)
        self.slider_pos.set(0)

        # Actualizar etiquetas de tiempo
        self.lbl_total.configure(text=self._fmt(self.video_duration))
        self.lbl_current.configure(text=self._fmt(0))
        self.lbl_duration.configure(
            text=f"Duración: {self._fmt(self.video_duration)}\n{self.video_fps:.2f} fps"
        )

        # Actualizar labels y barra de marcadores
        self.lbl_in.configure(text=f"{self.mark_in:.2f} s")
        self.lbl_out.configure(text=f"{self.mark_out:.2f} s")
        self._update_range_label()
        self._draw_markers()

        # Crear el reproductor para este video
        self.player = VideoPlayer(
            video_path        = self.selected_path,
            fps               = self.video_fps,
            duration          = self.video_duration,
            frame_callback    = self._on_new_frame,
            position_callback = self._on_position_update
        )

        # Habilitar todos los controles
        self._set_action_buttons("normal")
        self.btn_play.configure(state="normal")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="Video cargado ✓")

    # =========================================================================
    # Controles de reproducción
    # =========================================================================

    def toggle_play(self):
        """Alterna entre Play y Pause."""
        if self.player is None:
            return

        if self.player.is_playing:
            self.player.pause()
            self.btn_play.configure(text="▶")
        else:
            self.player.play()
            self.btn_play.configure(text="⏸")

    def stop_video(self):
        """Detiene la reproducción y regresa al inicio."""
        if self.player is None:
            return
        self.player.stop()
        self.btn_play.configure(text="▶")
        self.slider_pos.set(0)
        self.lbl_current.configure(text=self._fmt(0))
        # Cargar frame del inicio en un hilo para no bloquear la UI
        threading.Thread(
            target=self.engine.get_frame,
            args=(self.selected_path, 0.0, self._on_seek_frame),
            daemon=True
        ).start()

    def _on_scrub(self, value: float):
        """
        Llamado cada vez que el usuario mueve el slider de posición.
        Hace seek en el reproductor y carga el frame del punto exacto.
        """
        if self.player is None:
            return
        self.player.seek(value)
        self.lbl_current.configure(text=self._fmt(value))
        threading.Thread(
            target=self.engine.get_frame,
            args=(self.selected_path, value, self._on_seek_frame),
            daemon=True
        ).start()

    def _on_seek_frame(self, success: bool, result):
        """Callback del frame de seek → hilo principal."""
        if success:
            self.after(0, self._show_frame, result)

    # =========================================================================
    # Marcadores IN / OUT
    # =========================================================================

    def set_mark_in(self):
        """Establece el marcador IN en la posición actual del reproductor."""
        if self.player is None:
            return
        t = self.player.position
        if t >= self.mark_out:
            messagebox.showwarning(
                "Marcador inválido", "IN debe estar antes que OUT."
            )
            return
        self.mark_in = t
        self.lbl_in.configure(text=f"{t:.2f} s")
        self._update_range_label()
        self._draw_markers()

    def set_mark_out(self):
        """Establece el marcador OUT en la posición actual del reproductor."""
        if self.player is None:
            return
        t = self.player.position
        if t <= self.mark_in:
            messagebox.showwarning(
                "Marcador inválido", "OUT debe estar después de IN."
            )
            return
        self.mark_out = t
        self.lbl_out.configure(text=f"{t:.2f} s")
        self._update_range_label()
        self._draw_markers()

    def _update_range_label(self):
        """Actualiza la etiqueta con la duración del rango IN–OUT."""
        rng = max(0.0, self.mark_out - self.mark_in)
        self.lbl_range.configure(text=f"Rango: {self._fmt(rng)}")

    def _draw_markers(self):
        """
        Dibuja la barra visual de la línea de tiempo:
          - Fondo gris oscuro para toda la duración
          - Rango IN–OUT resaltado en azul
          - Línea verde para IN, línea roja para OUT
        """
        c = self.canvas_markers
        c.update_idletasks()
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 2 or self.video_duration <= 0:
            return

        c.delete("all")

        # Fondo
        c.create_rectangle(0, 0, w, h, fill="#2a2a2a", outline="")

        # Rango IN–OUT en azul
        x_in  = int(self.mark_in  / self.video_duration * w)
        x_out = int(self.mark_out / self.video_duration * w)
        c.create_rectangle(x_in, 0, x_out, h, fill="#1f6aa5", outline="")

        # Marcador IN (verde)
        c.create_line(x_in, 0, x_in, h, fill="#4caf50", width=2)

        # Marcador OUT (rojo)
        c.create_line(x_out, 0, x_out, h, fill="#f44336", width=2)

    # =========================================================================
    # Callbacks del reproductor (vienen de hilos secundarios)
    # =========================================================================

    def _on_new_frame(self, img: Image.Image):
        """Nuevo frame disponible en el reproductor → hilo principal."""
        self.after(0, self._show_frame, img)

    def _on_position_update(self, t: float):
        """Nueva posición desde el reproductor → hilo principal."""
        self.after(0, self._update_position_ui, t)

    def _update_position_ui(self, t: float):
        """Actualiza el slider y la etiqueta de tiempo (hilo principal)."""
        if self.video_duration > 0:
            self.slider_pos.set(t)
        self.lbl_current.configure(text=self._fmt(t))

        # Si el reproductor llegó al final, resetear el botón Play
        if t <= 0 and self.player and not self.player.is_playing:
            self.btn_play.configure(text="▶")

    def _show_frame(self, img: Image.Image):
        """
        Redimensiona el frame al tamaño actual del área de preview,
        manteniendo la relación de aspecto, y lo muestra en el label.
        Siempre debe llamarse desde el hilo principal.
        """
        self.preview_frame.update_idletasks()
        pw = self.preview_frame.winfo_width()  - 4
        ph = self.preview_frame.winfo_height() - 4
        if pw < 2 or ph < 2:
            pw, ph = 640, 360

        # Calcular escala manteniendo aspect ratio
        iw, ih = img.size
        scale  = min(pw / iw, ph / ih)
        nw     = int(iw * scale)
        nh     = int(ih * scale)

        resized = img.resize((nw, nh), Image.LANCZOS)

        # Guardar referencia para evitar garbage collection
        self._preview_image = ctk.CTkImage(resized, size=(nw, nh))
        self.img_label.configure(image=self._preview_image, text="")

    # =========================================================================
    # Exportación
    # =========================================================================

    def run_cut(self):
        """Solicita ruta de salida y lanza el corte entre IN y OUT en un hilo."""
        out = filedialog.asksaveasfilename(
            title="Guardar clip recortado",
            defaultextension=".mp4",
            filetypes=[("Video MP4", "*.mp4")]
        )
        if not out:
            return

        # Pausar reproducción antes de exportar
        if self.player and self.player.is_playing:
            self.player.pause()
            self.btn_play.configure(text="▶")

        self._set_processing_state(True)
        threading.Thread(
            target=self.engine.cut_logic,
            args=(self.selected_path, out, self.mark_in, self.mark_out,
                  self._on_task_finished),
            daemon=True
        ).start()

    def run_audio(self):
        """Solicita ruta de salida y lanza la extracción de audio en un hilo."""
        out = filedialog.asksaveasfilename(
            title="Guardar audio extraído",
            defaultextension=".mp3",
            filetypes=[("Audio MP3", "*.mp3")]
        )
        if not out:
            return

        self._set_processing_state(True)
        threading.Thread(
            target=self.engine.extract_audio_logic,
            args=(self.selected_path, out, self._on_task_finished),
            daemon=True
        ).start()

    def _on_task_finished(self, success: bool, message: str):
        """Callback de exportación → hilo principal."""
        self.after(0, self._finish_task_ui, success, message)

    def _finish_task_ui(self, success: bool, message: str):
        """Muestra el resultado y reabre los controles (hilo principal)."""
        self._set_processing_state(False)
        if success:
            self.progress.set(1)
            self.lbl_status.configure(text="Exportación completada ✓")
            messagebox.showinfo("¡Listo!", message)
            self._open_folder(os.path.dirname(self.selected_path))
        else:
            self.progress.set(0)
            self.lbl_status.configure(text="Error en la exportación ✗")
            messagebox.showerror("Error", message)

    # =========================================================================
    # Utilidades internas
    # =========================================================================

    def _set_action_buttons(self, state: str):
        """Habilita o deshabilita los botones de exportación y marcadores."""
        for btn in (self.btn_cut, self.btn_audio,
                    self.btn_set_in, self.btn_set_out):
            btn.configure(state=state)

    def _set_processing_state(self, processing: bool):
        """
        Bloquea / desbloquea la UI durante una exportación.
        Controla también la animación de la barra de progreso.
        """
        state = "disabled" if processing else "normal"
        self.btn_load.configure(state=state)
        self._set_action_buttons(state)
        self.btn_play.configure(state=state)
        self.btn_stop.configure(state=state)
        self.slider_pos.configure(state=state)

        if processing:
            self.progress.set(0)
            self.progress.start()
            self.lbl_status.configure(text="Exportando…")
        else:
            self.progress.stop()

    @staticmethod
    def _fmt(seconds: float) -> str:
        """
        Convierte segundos a formato m:ss.d
        Ejemplo: 75.3 → "1:15.3"
        """
        seconds = max(0.0, seconds)
        m = int(seconds // 60)
        s = seconds % 60
        return f"{m}:{s:04.1f}"

    @staticmethod
    def _open_folder(folder_path: str):
        """Abre el explorador en la carpeta indicada (Windows / macOS / Linux)."""
        if not folder_path or not os.path.isdir(folder_path):
            return
        if sys.platform == "win32":
            os.startfile(folder_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder_path])
        else:
            subprocess.Popen(["xdg-open", folder_path])

    def _on_close(self):
        """Detiene el reproductor limpiamente antes de cerrar la ventana."""
        if self.player:
            self.player.stop()
        self.destroy()


# =============================================================================
# Punto de entrada
# =============================================================================

if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    app = ArchitectVideoApp()
    app.mainloop()