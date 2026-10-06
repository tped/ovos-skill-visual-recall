from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
from ovos_workshop.skills import OVOSSkill
from ovos_bus_client.apis.ocp import OCPInterface
from ovos_utils.ocp import MediaType, PlaybackType, MediaEntry
from ovos_bus_client.message import Message

import os
import re
import json
import time
import filecmp
import subprocess

from .version import (
    VERSION_MAJOR,
    VERSION_MINOR,
    VERSION_BUILD,
    VERSION_ALPHA,
    VERSION_TAG
)

# Default settings
DEFAULT_SETTINGS = {
    "memories_data_path": "/home/ovos/NTR-Data/MeePiMemoryBank.json",
    "media_folder": "/home/ovos/MeePi_MemoryPalace",
    "display_time": 3,  # default seconds per image,
    "log_level": "DEBUG"
}

# Words used to interpret the reply to "see the images, hear the audio, or both?"
CHOICE_BOTH = {"both", "everything"}
CHOICE_YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "please"}
CHOICE_NONE = {"no", "nope", "nothing", "none", "neither", "cancel", "skip"}
CHOICE_IMAGES = {"see", "show", "look", "picture", "pictures", "image", "images",
                 "photo", "photos", "slideshow"}
# "here" is a likely STT mishearing of "hear"
CHOICE_AUDIO = {"hear", "here", "listen", "audio", "sound", "song", "music",
                "recording", "recordings", "play"}

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

        # Register OCP
        self.ocp = OCPInterface(self.bus)

        # Register event for NTR hand-offs
        self.add_event("visual.recall.display", self.handle_display_request)

        # Load paths & settings
        self.memories_data_path = self.settings.get("memories_data_path") or ""
        self.media_folder = self.settings.get("media_folder") or ""
        self.display_time = self.settings.get("display_time", 3)

        self.active_slideshow = False  # Initialize a flag for display loop
        self.audio_proc = None
        self.stop_requested = False

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

        # log and Speak version if log_level != INFO
        self.log.info(f"Visual Recall Skill version={self.skill_version()} tag='{VERSION_TAG}'")
        if self.log_level.upper() != "INFO":
            ver = self.skill_version()
            spoken_version = ver.replace("a", " alpha ")
            tag = f", {VERSION_TAG}" if VERSION_TAG else ""
            self.speak(
                f"MeePi Visual Recall, version {spoken_version}{tag}, initialized",
                wait=False
            )

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
        data = message.data or {}
        media_path = data.get("media_path") or ""
        memory_name = data.get("title") or "this memory"

        if not media_path or not os.path.exists(media_path):
            self.log.error(f"visual-recall: no media found at {media_path}")
            self.speak(f"I could not find any media for {memory_name}.")
            return

        self.log.info(f"visual-recall: received request to display media for {memory_name}")
        self._show_images(media_path, memory_name, filter_cover=True)

    # ----------------------
    # INTENT HANDLERS
    # ----------------------
    @intent_handler("PlayRecording.intent")
    def handle_play_recording_intent(self, message):
        if not self.enabled:
            self.speak("My visual recall isn't available right now.")
            return

        memory_name = message.data.get("query")
        if not memory_name:
            self.speak("I didn't catch what you wanted to hear.")
            return

        folder = self.find_matching_folder(memory_name)
        if not folder:
            self.speak(f"I couldn't find any audio for {memory_name}.")
            return

        self.stop_requested = False
        self._play_audio(folder, memory_name)

    # ----------------------
    # Audio Playback HELPERS
    # ----------------------

    def _play_audio(self, folder: str, memory_name: str):
        """Play every audio file in the folder, in filename order, via mpv (audio only)."""
        audio_files = self.get_audio_files(folder)
        if not audio_files:
            self.speak(f"I don't have any audio for {memory_name}, Tom.")
            return

        # Announce first and WAIT, so the speech is finished before the audio starts
        self.speak_dialog("playing_audio", {"memory_name": memory_name}, wait=True)

        for audio_path in audio_files:
            if self.stop_requested:
                break
            self.log.info(f"VR: Playing audio: {audio_path}")

            try:
                proc = subprocess.Popen(
                    ["mpv", "--no-video", "--really-quiet", audio_path]
                )
            except FileNotFoundError:
                self.log.error("VR: mpv not found on PATH")
                self.speak("I can't play audio right now, my player isn't installed.")
                return
            except Exception as e:
                self.log.error(f"VR: failed to start audio playback: {e}")
                self.speak("Something went wrong trying to play that, Tom.")
                return

            self.audio_proc = proc
            if self.stop_requested:  # stop() fired while we were starting up
                proc.terminate()
            proc.wait()  # blocks this handler thread, not the whole skill
            self.audio_proc = None

            if proc.returncode and proc.returncode > 0:
                self.log.warning(f"VR: mpv exited with code {proc.returncode} for {audio_path}")

        self.log.info("VR: Audio playback finished.")

    @intent_handler("MemoryPalace.intent")
    def handle_memory_palace_intent(self, message):
        if not self.enabled:
            self.speak("My visual recall isn't available right now.")
            return

        memory_name = message.data.get("query")
        if not memory_name:
            self.speak("I didn't catch the memory you're looking for.")
            return

        folder = self.find_matching_folder(memory_name)
        if not folder:
            self.speak(f"I couldn't find any media for {memory_name}.")
            return

        self.stop_requested = False

        # --------- 1. INVENTORY ----------
        images = self._collect_images(folder, filter_cover=False)
        audio_files = self.get_audio_files(folder)
        videos = self.get_video_files(folder)

        if not (images or audio_files or videos):
            self.speak(f"I couldn't find any media for {memory_name}.")
            return

        # --------- 2. DECIDE ----------
        show_images = bool(images)
        play_audio = bool(audio_files)

        if images and audio_files:  # only ask when there's a real choice
            found = self._describe_media(len(images), len(audio_files), len(videos))
            reply = self.get_response(
                "mixed_media_prompt",
                {"memory_name": memory_name, "found": found},
                num_retries=1,
            )
            if self.stop_requested:
                return

            choice = self._parse_media_choice(reply)
            self.log.info(f"VR: media choice reply={reply!r} -> {choice}")

            if choice == "none":
                self.speak("Okay.")
                return
            if choice == "unclear":
                self.speak("I didn't catch that, so I'll just show the images.")
                choice = "images"

            show_images = choice in ("images", "both")
            play_audio = choice in ("audio", "both")

        # --------- 3. PRESENT (fixed order: images, audio) ----------
        if show_images:
            self._show_images(folder, memory_name, filter_cover=False, images=images)

        if play_audio and not self.stop_requested:
            self._play_audio(folder, memory_name)

        # --------- 4. VIDEO (still a stub) ----------
        if videos and not self.stop_requested:
            self.speak("I also have a video memory but can't play it yet ... sorry")

    # ----------------------
    # MEDIA DISPLAY HELPERS
    # ----------------------
    @staticmethod
    def _parse_media_choice(reply):
        """Turn the spoken reply into 'images', 'audio', 'both', 'none' or 'unclear'."""
        words = set(re.findall(r"[a-z']+", (reply or "").lower()))
        wants_images = bool(words & CHOICE_IMAGES)
        wants_audio = bool(words & CHOICE_AUDIO)

        if words & CHOICE_BOTH or (wants_images and wants_audio):
            return "both"
        if wants_images:
            return "images"
        if wants_audio:
            return "audio"
        if words & CHOICE_NONE:
            return "none"
        if words & CHOICE_YES:
            return "both"
        return "unclear"

    @staticmethod
    def _describe_media(n_images, n_audio, n_videos):
        """Build a spoken summary like '4 images, 1 audio recording and 1 video'."""
        parts = []
        if n_images:
            parts.append(f"{n_images} image{'s' if n_images != 1 else ''}")
        if n_audio:
            parts.append(f"{n_audio} audio recording{'s' if n_audio != 1 else ''}")
        if n_videos:
            parts.append(f"{n_videos} video{'s' if n_videos != 1 else ''}")
        if len(parts) > 1:
            return ", ".join(parts[:-1]) + " and " + parts[-1]
        return parts[0] if parts else ""

    def _collect_images(self, folder: str, filter_cover: bool = True):
        """Return the unique images to show (same cover/clone rules as before)."""
        all_images = self.get_media_files(folder)

        cover_path = next((img for img in all_images
                           if os.path.splitext(os.path.basename(img))[0].lower().strip() == "cover"), None)

        images = []
        for img in all_images:
            is_cover_file = (img == cover_path)

            # If NTR called this, skip the actual cover file
            if filter_cover and is_cover_file:
                continue

            # ALWAYS skip byte-for-byte clones of the cover (The Plymouth Fix)
            if not is_cover_file and cover_path and filecmp.cmp(img, cover_path, shallow=False):
                self.log.info(f"VR: Skipping duplicate clone: {os.path.basename(img)}")
                continue

            images.append(img)
        return images
    
    def _show_images(self, folder: str, memory_name: str, filter_cover: bool = True, images=None):
        """Display unique images. If filter_cover is True, skips the specific 'cover.jpg' file."""
        if images is None:  # NTR hand-off path: build the list ourselves
            if not self.get_media_files(folder):
                self.speak_dialog("no_image_found", {"memory_name": memory_name})
                return
            images = self._collect_images(folder, filter_cover)

        # Identify the cover file & Build List of media now handled in above/seperate routines

        image_count = len(images)
        if image_count == 0:
            if filter_cover:  # NTR scenario
                self.log.info("VR: No additional unique images to show.")
            else:  # Standalone scenario
                self.speak(f"I don't have any images for {memory_name}, Tom.")
            return

        # Total duration = (number of images * display time) + (estimated speech time)
        # We'll use 3 seconds as a "speech buffer" per chatter instance
        speech_buffers = (image_count // 2) * 3
        total_session_time = (image_count * self.display_time) + speech_buffers + 10

        if not self.gui:
            self.log.error("VR: GUI not available, can't show images")
            self.speak("I can't reach my display right now.")
            return

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
        self.active_slideshow = False  # always clear the flag, GUI or not
        if not self.gui:
            return
        try:
            self.gui.override_idle = False
            self.gui.remove_page("ImagePage")
            self.gui.release()
        except Exception as e:
            self.log.warning(f"GUI release failed: {e}")

    def _file2entry(self, file_path: str, media_type: MediaType):
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

        # AGGRESSIVE SANITIZATION
        # Remove punctuation (commas, dots, etc.)
        clean_name = re.sub(r'[^\w\s]', '', memory_name.lower())
        # Replace spaces/multiple spaces with a single underscore
        target_name = re.sub(r'\s+', '_', clean_name).strip('_')
        all_folders = os.listdir(self.media_folder)

        # --- TIER 1: Exact Match ---
        # Good for legacy folders or non-timestamped entries
        for folder in all_folders:
            if folder.lower() == target_name:
                self.log.info(f"VR: Exact folder match found: {folder}")
                return os.path.join(self.media_folder, folder)

        # --- TIER 2: Timestamped Suffix Match (The NTR Special) ---
        # Specifically looks for YYYYMMDDHHMMSS_target_name
        for folder in all_folders:
            if "_" in folder:
                # Splits once at the first underscore to separate timestamp from title
                parts = folder.lower().split("_", 1)
                if len(parts) > 1 and parts[1] == target_name:
                    self.log.info(f"VR: Timestamped title match found: {folder}")
                    return os.path.join(self.media_folder, folder)

        # --- TIER 3: Fallback Partial Match ---
        # The "Fuzzy" catch-all if Tier 1 and 2 fail
        for folder in all_folders:
            if target_name in folder.lower():
                self.log.debug(f"VR: Partial match fallback found: {folder}")
                return os.path.join(self.media_folder, folder)

        # DEBUG: Using a fixed string since 'folder_name' scope is loop-dependent
        self.log.debug(f"No match found for '{target_name}' in {self.media_folder}")
        return None

    # ----------------------
    # STOP HANDLER
    # ----------------------
    def stop(self):
        """Stop anything currently playing."""
        stopped = False

        # Tell any in-progress memory palace flow not to move on to its next stage
        self.stop_requested = True

        if self.active_slideshow:
            self.active_slideshow = False
            self._release_gui()
            stopped = True

        proc = self.audio_proc
        if proc is not None:
            proc.terminate()
            self.audio_proc = None
            stopped = True

        return stopped
