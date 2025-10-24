from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
# from ovos_workshop.intents import IntentHandler # Uncomment to use Adapt intents
from ovos_workshop.skills import OVOSSkill
# NEW imports for OCP media
from ovos_bus_client.apis.ocp import OCPInterface
from ovos_utils.ocp import MediaType, PlaybackType, MediaEntry

import os
import json
import time

# Populate settings.json with default values, do so here
DEFAULT_SETTINGS = {
    "memories_data_path": "/home/ovos/NTR-Data/MeePiMemoryBank.json",
    "media_folder": "/home/ovos/MeePi_Media",
    "display_time": 3  # default seconds per image
}


class VisualRecallSkill(OVOSSkill):
    def __init__(self, *args, **kwargs):
        """The __init__ method is called when the Skill is first constructed.
        Note that self.bus, self.skill_id, self.settings, and
        other base class settings are only available after the call to super().
        """
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

    def initialize(self):
        # merge default settings
        # self.settings is a jsondb, which extends the dict class and adds helpers like merge
        self.settings.merge(DEFAULT_SETTINGS, new_only=True)

        self.ocp = OCPInterface(self.bus)  # OCP registration

        # Load settings from self.settings
        self.memories_data_path = self.settings.get("memories_data_path")
        self.media_folder = self.settings.get("media_folder")
        self.display_time = self.settings.get("display_time", 3)

        self.enabled = True  # an optimist!

        # Initialize with paths to memories and media.

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
            self.media_available = False
            self.enabled = False

        # Notify the user if something went wrong
        if not self.enabled:
            self.speak_dialog("Visual Recall had an initialization error")
        else:
            self.speak("Visual Recall is Alive - Phase 2/4 - OCP register only once")

    @property
    def my_setting(self):
        """Dynamically get the my_setting from the skill settings file.
        If it doesn't exist, return the default value.
        This will reflect live changes to settings.json files (local or from backend)
        """
        return self.settings.get("my_setting", "default_value")

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
        images = self.get_media_files(folder)
        # skip cover (it's a duplicate of one of the images)
        images = [img for img in images if os.path.splitext(os.path.basename(img).lower())[0] != "cover"]

        if images:
            self.speak_dialog("show_all_images", {"memory_name": memory_name, "count": len(images)})
            for idx, img in enumerate(images, start=1):
                if not os.path.exists(img):
                    continue
                self.gui.show_image(img, fill='PreserveAspectFit')
                time.sleep(self.display_time)
                if idx % 2 == 0 or len(images) <= 4:
                    self.speak_dialog("heres_another_image")
                    time.sleep(0.5)

            self.speak_dialog("end_of_images", {"memory_name": memory_name})

        # --- VIDEOS ---
        videos = self.get_video_files(folder)
        if videos:
            self.speak_dialog("playing_videos", {"memory_name": memory_name})
            for vid in videos:
                if not os.path.exists(vid):
                    continue
                self.log.info(f"Playing video: {vid}")
                from ovos_utils.ocp import MediaEntry, PlaybackType, MediaType
                entry = MediaEntry(
                    title=os.path.basename(vid),
                    uri="file://" + vid,
                    playback=PlaybackType.VIDEO,
                    media_type=MediaType.VIDEO,
                    skill_id=self.skill_id,
                    skill_icon=""
                )
                self.ocp.play([entry])

        # --- AUDIO Files ---
        audio_files = self.get_audio_files(folder)
        if audio_files:
            self.speak_dialog("playing_audio", {"memory_name": memory_name})
            for aud in audio_files:
                if not os.path.exists(aud):
                    continue
                self.log.info(f"Playing audio: {aud}")
                from ovos_utils.ocp import MediaEntry, PlaybackType, MediaType
                entry = MediaEntry(
                    title=os.path.basename(aud),
                    uri="file://" + aud,
                    playback=PlaybackType.AUDIO,
                    media_type=MediaType.MUSIC,
                    skill_id=self.skill_id,
                    skill_icon=""
                )
                self.ocp.play([entry])

    def show_memory_images(self, memory_name):
        """
        Display all images in a memory folder sequentially.
        :param memory_name: name of the memory to recall
        """
        folder = self.find_matching_folder(memory_name)
        if not folder:
            self.speak(f"I couldn't find any media for {memory_name}.")
            return

        # Grab all images in folder
        images = self.get_media_files(folder)
        if not images:
            self.speak_dialog("no_image_found", {"memory_name": memory_name})
            return

        # Remove cover image regardless of extension
        images = [
            img for img in images
            if os.path.splitext(os.path.basename(img).lower())[0] != "cover"
        ]

        # Speak how many images are available
        image_count = len(images)
        # self.speak(f"I found {len(images)} images from {memory_name}.")
        self.log.info(f"Displaying images from {folder}: {images}")

        if image_count == 1:
            self.speak_dialog("show_image", {"memory_name": memory_name})
        else:
            self.speak_dialog("show_all_images",
                              {"memory_name": memory_name, "count": image_count})
        # Sequentially display images
        for idx, img_path in enumerate(images, start=1):
            if not os.path.exists(img_path):
                continue
            self.log.info(f"Displaying image {idx}/{len(images)}: {img_path}")
            self.gui.show_image(img_path, fill='PreserveAspectFit')
            time.sleep(self.display_time)

            # Add variety during sequence
            if image_count > 1 and idx < image_count:
                # Randomly add some chatter every few images
                if idx % 2 == 0 or image_count <= 4:
                    self.speak_dialog("heres_another_image")
                    time.sleep(0.5)

        self.speak_dialog("end_of_images", {"memory_name": memory_name})

    def find_matching_folder(self, memory_name):
        target_name = memory_name.lower().replace(" ", "_")
        for folder_name in os.listdir(self.media_folder):
            if folder_name.lower() == target_name:
                return os.path.join(self.media_folder, folder_name)

        # Optional: fallback to partial match
        for folder_name in os.listdir(self.media_folder):
            if target_name in folder_name.lower():
                return os.path.join(self.media_folder, folder_name)

        return None

    def _file2entry(self, file_path, media_type):
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

    def get_media_files(self, folder):
        valid_ext = ('.jpg', '.jpeg', '.png', '.gif')
        return [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.lower().endswith(valid_ext)
        ]

    def get_video_files(self, folder):
        valid_ext = ('.mp4', '.mkv', '.avi', '.mov', '.webm')
        return [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.lower().endswith(valid_ext)
        ]

    def get_audio_files(self, folder):
        valid_ext = ('.mp3', '.wav', '.flac', '.m4a', '.aac')
        return [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.lower().endswith(valid_ext)
        ]

    def stop(self):
        """Optional action to take when "stop" is requested by the user.
        This method should return True if it stopped something or
        False (or None) otherwise.
        If not relevant to your skill, feel free to remove.
        """
        return
