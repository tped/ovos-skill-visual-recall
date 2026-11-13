from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
from ovos_workshop.skills import OVOSSkill
# from ovos_bus_client.apis.ocp import OCPInterface
from ovos_utils.ocp import MediaType, PlaybackType, MediaEntry
from ovos_bus_client.message import Message, dig_for_message

import os
import re
import json
import time
import filecmp
import subprocess
import threading
from pathlib import Path

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
CHOICE_BOTH = {"both", "everything", "all"}
CHOICE_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "yesss"}
CHOICE_NONE = {"no", "nope", "nah", "nothing", "none", "neither", "cancel", "skip"}
CHOICE_IMAGES = {"see", "show", "look", "picture", "pictures", "image", "images",
                 "photo", "photos", "slideshow"}
# "here" is a likely STT mishearing of "hear"
CHOICE_AUDIO = {"hear", "here", "listen", "audio", "sound", "song", "music",
                "recording", "recordings", "play"}
# Choices for video playback
CHOICE_VIDEO = {"watch", "video", "videos", "movie", "movies", "clip", "clips", "film"}
# Words that skip the current track or video when heard during playback
SKIP_WORDS = {"skip", "next"}

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

        # Register event for NTR hand-offs
        self.add_event("visual.recall.display", self.handle_display_request)

        # Load paths & settings
        self.memories_data_path = self.settings.get("memories_data_path") or ""
        self.media_folder = self.settings.get("media_folder") or ""
        self.display_time = self.settings.get("display_time", 3)

        self.active_slideshow = False  # Initialize a flag for display loop
        self.audio_proc = None
        self.stop_requested = False
        self.audio_active = False  # True while a track queue is running
        self.skip_requested = False
        self.presenting = False  # True for the whole _present_memory flow, including the prompt
        self.stop_event = threading.Event()  # set by stop(); lets sleeping loops wake up immediately

        self.video_active = False
        self.video_done = threading.Event()
        self.add_event("visual.recall.play_video", self.handle_play_video_test)
        if self.gui:
            self.gui.register_handler("visual.recall.video.done", self.handle_video_done)

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
        self._present_memory(media_path, memory_name, filter_cover=True)

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

    @intent_handler("SkipTrack.intent")
    def handle_skip(self, _message):
        if self.video_active:
            self.video_done.set()
            return
        if not self.audio_active:
            self.speak("Nothing is playing right now.")
            return
        self.skip_requested = True
        proc = self.audio_proc
        if proc is not None:
            proc.terminate()  # during an announcement there's no mpv; the flag does the work

    def converse(self, message: Message = None) -> bool:
        """Catch skip words during playback, even when STT adds noise ('hahaha skip')."""
        if not (self.audio_active or self.video_active):
            return False
        data = (message.data if message else None) or {}
        utterances = data.get("utterances") or []
        words = set()
        for utt in utterances:
            words |= set(re.findall(r"[a-z']+", utt.lower()))
        if words & SKIP_WORDS:
            self.handle_skip(message)
            return True  # consumed, so it never falls through to the persona
        return False

    # ----------------------
    # Audio Playback HELPERS
    # ----------------------

    def _play_audio(self, folder: str, memory_name: str):
        """Play the audio files in filename order, announcing each one.
        'skip' ends the current track (or announcement) and moves to the next."""
        audio_files = self.get_audio_files(folder)
        if not audio_files:
            self.speak(f"I don't have any audio for {memory_name}, Tom.")
            return

        total = len(audio_files)
        dialog = "playing_audio_multi" if total > 1 else "playing_audio"
        self.speak_dialog(dialog, {"memory_name": memory_name}, wait=True)

        self.audio_active = True
        try:
            for idx, audio_path in enumerate(audio_files, start=1):
                if self.stop_requested:
                    break
                self.skip_requested = False

                title = self._speakable_title(audio_path)
                if total > 1:
                    self.speak(f"Number {idx} of {total}: {title}", wait=True)
                else:
                    self.speak(title, wait=True)

                if self.stop_requested:
                    break
                if self.skip_requested:  # skipped during the announcement
                    continue

                self.log.info(f"VR: Playing audio {idx}/{total}: {audio_path}")
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
                if self.stop_requested or self.skip_requested:  # arrived while starting up
                    proc.terminate()
                proc.wait()
                self.audio_proc = None

                # 4 = mpv ended by a signal (our terminate()), which is normal
                if proc.returncode not in (0, 4) and not (self.stop_requested or self.skip_requested):
                    self.log.warning(f"VR: mpv exited with code {proc.returncode} for {audio_path}")
        finally:
            self.audio_active = False
            self.skip_requested = False

        self.log.info("VR: Audio playback finished.")

    def handle_play_video_test(self, message):  # dev hook, run on its own thread
        path = (message.data or {}).get("path") or ""
        if path and Path(path).is_file():
            threading.Thread(target=self._play_video, args=(path,), daemon=True).start()
        else:
            self.log.warning(f"VR: play_video test hook got no usable path: {path!r}")

    def handle_video_done(self, _message=None):
        self.video_done.set()

    def _play_video(self, path, release=True):
        if not self.gui:
            self.speak("I can't reach my display right now.")
            return
        self.video_done.clear()
        self.video_active = True
        try:
            self.gui["videoSource"] = Path(os.path.abspath(path)).as_uri()
            self.gui.show_page("VideoPlayer", override_idle=True)
            self.video_done.wait(timeout=1800)  # QML's done event, skip or stop ends it sooner
        finally:
            self.video_active = False
            if release:
                self._release_gui()

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

        self._present_memory(folder, memory_name, filter_cover=False)

    # ----------------------
    # MEDIA DISPLAY HELPERS
    # ----------------------
    @staticmethod
    def _parse_media_choice(reply):
        """Turn the spoken reply into 'images', 'audio', 'video', 'both', 'none' or 'unclear'."""
        # Squeeze stretched STT output ("yesssss", "noooo") down to one letter
        text = re.sub(r"(.)\1{2,}", r"\1", (reply or "").lower())
        words = set(re.findall(r"[a-z']+", text))
        wants_images = bool(words & CHOICE_IMAGES)
        wants_audio = bool(words & CHOICE_AUDIO)
        wants_video = bool(words & CHOICE_VIDEO)

        if words & CHOICE_BOTH or (wants_images + wants_audio + wants_video) >= 2:
            return "both"
        if wants_images:
            return "images"
        if wants_video:
            return "video"
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

    def _activate_for_converse(self):
        """Make VR the active skill so get_response replies are delivered to us.

        NTR hand-offs arrive via a bus event, so VR was never activated by an intent
        match. The context skill_id must be VR's own or the converse service refuses
        the request, so we force it instead of trusting the incoming message.
        """
        msg = dig_for_message() or Message("")
        activate_msg = msg.forward(
            "intent.service.skills.activate",
            data={"skill_id": self.skill_id, "timeout": 5},  # minutes
        )
        activate_msg.context = {**(activate_msg.context or {}), "skill_id": self.skill_id}
        self.bus.emit(activate_msg)

    def _present_memory(self, folder: str, memory_name: str, filter_cover: bool):
        """Guard around the real flow so 'presenting' can never get stuck True."""
        if self.presenting:
            self.log.warning("VR: already presenting a memory, ignoring new request")
            return
        self.presenting = True
        self.stop_event.clear()
        # noinspection PyBroadException
        try:
            self._present_memory_flow(folder, memory_name, filter_cover)
        except Exception:
            self.log.exception("VR: presentation failed, releasing the screen")
            self._release_gui()
        finally:
            self.presenting = False

    def _present_memory_flow(self, folder: str, memory_name: str, filter_cover: bool):
        self.stop_requested = False
        self._activate_for_converse()

        images = self._collect_images(folder, filter_cover=filter_cover)
        audio_files = self.get_audio_files(folder)
        videos = self.get_video_files(folder)

        if not (images or audio_files or videos):
            if filter_cover:
                self.log.info("VR: No additional media to present.")
            else:
                self.speak(f"I couldn't find any media for {memory_name}.")
            return

        show_images = bool(images)
        play_audio = bool(audio_files)
        play_video = bool(videos)

        # Ask when there's a real choice (more than one kind of media), or when NTR
        # handed off (the user hasn't asked for anything yet, so offer the extras once)
        kinds = sum(bool(x) for x in (images, audio_files, videos))
        if kinds > 1 or (filter_cover and kinds):
            if kinds > 1:
                dialog = "mixed_media_prompt"
            elif images:
                dialog = "images_only_prompt"
            elif audio_files:
                dialog = "audio_only_prompt"
            else:
                dialog = "video_only_prompt"
            found = self._describe_media(len(images), len(audio_files), len(videos))

            reply = self.get_response(
                dialog,
                {"memory_name": memory_name, "found": found},
                num_retries=1,
            )
            if self.stop_requested:
                self._release_gui()
                return

            choice = self._parse_media_choice(reply)
            self.log.info(f"VR: media choice reply={reply!r} -> {choice}")

            if choice in ("none", "unclear"):
                if choice == "none":
                    self.speak("Okay.")
                else:
                    self.speak("I didn't catch that, so I'll leave it there.")
                self._release_gui()
                return

            show_images = bool(images) and choice in ("images", "both")
            play_audio = bool(audio_files) and choice in ("audio", "both")
            play_video = bool(videos) and choice in ("video", "both")

        if show_images:
            self._show_images(folder, memory_name, filter_cover=filter_cover, images=images)

        if play_audio and not self.stop_requested:
            self._show_cover(folder)
            self._play_audio(folder, memory_name)

        if play_video and not self.stop_requested:
            dialog = "playing_video_multi" if len(videos) > 1 else "playing_video"
            self.speak_dialog(dialog, {"memory_name": memory_name}, wait=True)
            for video_path in videos:
                if self.stop_requested:
                    break
                self._play_video(video_path, release=False)

        self._release_gui()  # hand-offs leave NTR's cover up; let the screen return to idle

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

            if self.stop_event.wait(self.display_time):  # True means stop() fired during the wait
                break

            # Add chatter every few images
            if image_count > 1 and idx < image_count:
                if idx % 2 == 0 or image_count <= 4:
                    self.speak_dialog("heres_another_image", wait=True)
                    time.sleep(0.5)

        self.log.info("Slideshow finished. Cleaning up.")
        was_stopped = self.stop_event.is_set()

        # DO NOT Release GUI after images, we'll let _present_memory_flow do it when done
        # self._release_gui()
        # DO set active_slideshow to False - indicate end of images
        self.active_slideshow = False

        if not was_stopped:
            self.speak_dialog("end_of_images", {"memory_name": memory_name})

    def _release_gui(self):
        """Safely release GUI to prevent player lockout."""
        self.active_slideshow = False  # always clear the flag, GUI or not
        if not self.gui:
            return
        try:
            self.gui.override_idle = False
            self.gui.remove_page("ImagePage")
            self.gui.remove_page("VideoPlayer")
            self.gui.release()
        except Exception as e:
            self.log.warning(f"GUI release failed: {e}")

    def _show_cover(self, folder: str):
        """Put the memory's cover image back on screen (held until the next _release_gui)."""
        if not self.gui:
            return
        for img in self.get_media_files(folder):
            if os.path.splitext(os.path.basename(img))[0].lower().strip() == "cover":
                self.gui.show_image(img, fill='PreserveAspectFit', override_idle=True)
                return

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
        valid_ext = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v')
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
               and os.path.getsize(os.path.join(folder, f)) > 0  # skip empty/failed copies
        ]

    @staticmethod
    def _speakable_title(path: str) -> str:
        """Spoken name for a media file, from its filename.
        Convention: 'Title - Artist.ext' is spoken as 'Title by Artist'."""
        name = os.path.splitext(os.path.basename(path))[0]
        name = re.sub(r"^\d{1,3}[\s._-]+", "", name)   # leading track number from old rips
        name = name.replace("_", " ")
        name = re.sub(r"[()\[\]]", " ", name)          # '(live)' reads better as plain words
        if " - " in name:
            title, artist = name.rsplit(" - ", 1)      # split on the LAST ' - ' so titles can contain one
            name = f"{title.strip()} by {artist.strip()}"
        return re.sub(r"\s+", " ", name).strip() or "this recording"

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
    # HANDLE STOP
    # ----------------------
    def can_stop(self, _message: Message) -> bool:
        """Tell the stop pipeline whether VR has anything running to stop."""
        busy = bool(self.presenting or self.active_slideshow or self.audio_active or self.video_active or self.audio_proc is not None)
        self.log.debug(f"VR: can_stop -> {busy}")
        return busy

    def stop(self):
        """Stop anything currently playing."""
        stopped = False

        # Tell any in-progress memory palace flow not to move on to its next stage
        self.stop_requested = True
        self.stop_event.set()
        self.bus.emit(Message("mycroft.audio.speech.stop"))

        if self.active_slideshow:
            self.active_slideshow = False
            self._release_gui()
            stopped = True

        if self.video_active:
            self.video_done.set()
            self._release_gui()
            stopped = True

        proc = self.audio_proc
        if proc is not None:
            if proc is not None:
                proc.terminate()
                self.audio_proc = None
                stopped = True

        if self.audio_active:  # between tracks or mid-announcement: no mpv, but the loop is ending
            stopped = True

        if self.presenting:  # e.g. waiting on the media prompt; stop_requested ends the flow
            stopped = True

        return stopped
