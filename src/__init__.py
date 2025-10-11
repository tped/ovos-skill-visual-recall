from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
# from ovos_workshop.intents import IntentHandler # Uncomment to use Adapt intents
from ovos_workshop.skills import OVOSSkill

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
            self.speak("Visual Recall is Alive - Phase 1/0 - Display images in directory")

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

        self.show_memory_images(memory_name)

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
            self.speak(f"I remember {memory_name} but have no visual memories")
            return

        # Remove cover image regardless of extension
        images = [
            img for img in images
            if os.path.splitext(os.path.basename(img).lower())[0] != "cover"
        ]

        # Speak how many images are available
        self.speak(f"I found {len(images)} images from {memory_name}.")
        self.log.info(f"Displaying images from {folder}: {images}")

        # Sequentially display images
        for idx, img_path in enumerate(images, start=1):
            if not os.path.exists(img_path):
                continue
            self.log.info(f"Displaying image {idx}/{len(images)}: {img_path}")
            self.gui.show_image(img_path, fill='PreserveAspectFit')
            time.sleep(self.display_time)

        self.speak("That's all the images I found.")

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

    def get_media_files(self, folder):
        valid_ext = ('.jpg', '.jpeg', '.png', '.gif')
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
