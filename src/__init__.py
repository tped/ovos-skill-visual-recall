from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
from ovos_workshop.skills import OVOSSkill
from ovos_bus_client.apis.ocp import OCPInterface
from ovos_utils.ocp import MediaType, PlaybackType, MediaEntry
from ovos_bus_client.message import Message

import os
import json
import time

from .version import (
    VERSION_MAJOR,
    VERSION_MINOR,
    VERSION_BUILD,
    VERSION_ALPHA
)

# Default settings
DEFAULT_SETTINGS = {
    "memories_data_path": "/home/ovos/NTR-Data/MeePiMemoryBank.json",
    "media_folder": "/home/ovos/MeePi_MemoryPalace",
    "display_time": 3,  # default seconds per image,
    "log_level": "WARNING"
}


class VisualRecallSkill(OVOSSkill):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.learning = True

    @classproperty
    def runtime_requirements(self):
        return RuntimeRequirements(
            internet_before_load=False,
            network_before_load=False,
            gui_before_load=True,
            requires_internet=False,
            requires_network=False,
            requires_gui=True,
            no_internet_fallback=False,
            no_network_fallback=False,
            no_gui_fallback=True,
        )

    @staticmethod
    def skill_version():
        version_string = f"{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_BUILD}"
        if VERSION_ALPHA and int(VERSION_ALPHA) > 0:
            version_string += f"a{VERSION_ALPHA}"
        return version_string
    # ----------------------
    # INITIALIZATION
    # ----------------------

    def initialize(self):
        # merge default settings
        # self.settings is a jsondb, which extends the dict class and adds helpers like merge
        self.settings.merge(DEFAULT_SETTINGS, new_only=True)
        self.log_level = self.settings.get("log_level", "INFO")

        # Speak version if log_level != INFO
        if self.log_level.upper() != "INFO":
            ver = self.skill_version()
            spoken_version = ver.replace("a", " alpha ")
            self.speak(
                f"MeePi Visual Recall, version {spoken_version}, initialized",
                wait=False
            )

        # Register OCP
        self.ocp = OCPInterface(self.bus)

        # Register event for NTR hand-offs
        self.add_event("visual.recall.display", self.handle_display_request)

        # Load paths & settings
        self.memories_data_path = self.settings.get("memories_data_path")
        self.media_folder = self.settings.get("media_folder")
        self.display_time = self.settings.get("display_time", 3)

        self.active_slideshow = False  # Initialize a flag for display loop

        self.enabled = True

        # Load memory JSON
        try:
            with open(self.memories_data_path, 'r', encoding='utf-8') as f:
                self.memory_data = json.load(f)
            self.log.info(f"Loaded {len(self.memory_data)} memories from JSON.")
        except Exception as e:
            self.log.error(f"Failed to load memory JSON: {e}")
            self.memory_data = None
            self.enabled = False

        if not os.path.isdir(self.media_folder):
            self.log.error(f"Media folder does not exist: {self.media_folder}")
            self.enabled = False

        if not self.enabled:
            self.speak_dialog("MeePi's Visual Recall had an initialization error")

        self.log.info(f"Visual Recall Skill version={self.skill_version()}")

    # ----------------------
    # SETTINGS HELPERS
    # ----------------------
    @property
    def my_setting(self):
        return self.settings.get("my_setting", "default_value")

    # -----------------------
    # REQUEST HANDLER for NTR
    # -----------------------
    def handle_display_request(self, message: Message):
        media_path = message.data.get("media_path")
        memory_name = message.data.get("title") or "this memory"

        if not media_path or not os.path.exists(media_path):
            self.log.error(f"visual-recall: no media found at {media_path}")
            self.speak(f"I could not find any media for {memory_name}.")
            return

        self.log.info(f"visual-recall: received request to display media for {memory_name}")
        self._show_images(media_path, memory_name)

    # ----------------------
    # INTENT HANDLER
    # ----------------------
    @intent_handler("MemoryPalace.intent")
    def handle_memory_palace_intent(self, message):
        memory_name = message.data.get("query")
        if not memory_name:
            self.speak("I didn't catch the memory you're looking for.")
            return

        folder = self.find_matching_folder(memory_name)
        if not folder:
            self.speak(f"I couldn't find any media for {memory_name}.")
            return

        # --------- IMAGES ----------
        self._show_images(folder, memory_name)

        # --------- VIDEOS ----------
        videos = self.get_video_files(folder)
        if videos:
            self.speak_dialog("playing_videos", {"memory_name": memory_name})
            for vid in videos:
                if not os.path.exists(vid):
                    continue
                self.log.info(f"Playing video: {vid}")
                entry = self._file2entry(vid, MediaType.VIDEO)
                self.speak(f"I have a video memory but can't play it yet ... sorry")
                # self.ocp.play([entry])

        # --------- AUDIO ----------
        audio_files = self.get_audio_files(folder)
        if audio_files:
            self.speak_dialog("playing_audio", {"memory_name": memory_name})
            for aud in audio_files:
                if not os.path.exists(aud):
                    continue
                self.log.info(f"Playing audio: {aud}")
                entry = self._file2entry(aud, MediaType.AUDIO)
                self.speak(f"I have an audio memory but can't play it yet ... sorry")
                # self.ocp.play([entry])

    # ----------------------
    # MEDIA DISPLAY HELPERS
    # ----------------------
    def _show_images(self, folder: str, memory_name: str):
        """Display all images in a memory folder sequentially with GUI release."""
        images = self.get_media_files(folder)
        if not images:
            self.speak_dialog("no_image_found", {"memory_name": memory_name})
            return

        # Remove cover image and prepare for slideshow
        images = [
            img for img in images
            if os.path.splitext(os.path.basename(img).lower())[0] != "cover"
        ]
        image_count = len(images)
        # Total duration = (number of images * display time) + (estimated speech time)
        # We'll use 3 seconds as a "speech buffer" per chatter instance
        speech_buffers = (image_count // 2) * 3
        total_session_time = (image_count * self.display_time) + speech_buffers + 10

        # --- THE FIX: lock idle for expected duration of the show
        self.gui.override_idle = total_session_time
        self.active_slideshow = True

        if image_count == 1:
            self.speak_dialog("show_image", {"memory_name": memory_name})
        else:
            self.speak_dialog("show_all_images", {"memory_name": memory_name, "count": image_count})

        for idx, img_path in enumerate(images, start=1):
            # Check if stop() was called while we were sleeping
            if not self.active_slideshow:
                break

            # Remaining time for this specific image's 'KeepAlive'
            remaining_time = total_session_time - (idx * self.display_time)

            self.log.info(f"Displaying {idx}/{image_count}. Lock remaining: {remaining_time}s")

            if not os.path.exists(img_path):
                continue
            self.log.info(f"Displaying image {idx}/{image_count}: {img_path}")
            self.gui.show_image(img_path, fill='PreserveAspectFit', override_idle=remaining_time)

            time.sleep(self.display_time)

            # Add chatter every few images
            if image_count > 1 and idx < image_count:
                if idx % 2 == 0 or image_count <= 4:
                    self.speak_dialog("heres_another_image", wait=True)
                    time.sleep(0.5)

        self.log.info("Slideshow finished. Cleaning up.")

        # Release GUI after images
        self._release_gui()

        self.speak_dialog("end_of_images", {"memory_name": memory_name})

    def _release_gui(self):
        """Safely release GUI to prevent player lockout."""
        try:
            self.active_slideshow = False
            self.gui.override_idle = False  # Tells the bus 'I am done with the timer'
            self.gui.remove_page("ImagePage")  # Optional: specific cleanup if needed
            self.gui.release()
        except Exception as e:
            self.log.warning(f"GUI release failed: {e}")

    def _file2entry(self, file_path, media_type):
        """Convert a file path into a MediaEntry for OCP playback."""
        file_path = os.path.expanduser(file_path)
        if not file_path.startswith("file://"):
            file_path = "file://" + file_path

        if media_type == MediaType.AUDIO:
            playback_type = PlaybackType.AUDIO
        else:
            playback_type = PlaybackType.VIDEO

        return MediaEntry(
            title=os.path.basename(file_path),
            media_type=media_type,
            playback=playback_type,
            uri=file_path,
            match_confidence=100,
            skill_id=self.skill_id,
            skill_icon="",
            length=0
        )

    # ----------------------
    # MEDIA FILE LIST HELPERS
    # ----------------------
    @staticmethod
    def get_media_files(folder: str):
        valid_ext = ('.jpg', '.jpeg', '.png', '.gif')
        return [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.lower().endswith(valid_ext)
        ]

    @staticmethod
    def get_video_files(folder: str):
        valid_ext = ('.mp4', '.mkv', '.avi', '.mov', '.webm')
        return [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.lower().endswith(valid_ext)
        ]

    @staticmethod
    def get_audio_files(folder: str):
        valid_ext = ('.mp3', '.wav', '.flac', '.m4a', '.aac')
        return [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.lower().endswith(valid_ext)
        ]

    # ----------------------
    # MEMORY FOLDER HELPERS
    # ----------------------
    def find_matching_folder(self, memory_name):
        # DEBUG: Only shows up if you specifically turn on Debugging
        self.log.debug(f"Searching for folder matching: {memory_name}")

        folder_name = "N/A"
        target_name = memory_name.lower().replace(" ", "_")

        for folder_name in os.listdir(self.media_folder):
            if folder_name.lower() == target_name:
                return os.path.join(self.media_folder, folder_name)
        # fallback partial match
        for folder_name in os.listdir(self.media_folder):
            if target_name in folder_name.lower():
                return os.path.join(self.media_folder, folder_name)

        # DEBUG: Tell us why it failed only if we are looking for it
        self.log.debug(f"No match found for '{target_name}' in '{folder_name}'")
        return None

    # ----------------------
    # STOP HANDLER
    # ----------------------
    def stop(self):
        """Stop anything currently playing."""
        self.active_slideshow = False  # This breaks the loop in _show_images
        self._release_gui()
        return True
