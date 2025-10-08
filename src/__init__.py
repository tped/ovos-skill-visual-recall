from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
from ovos_workshop.skills import OVOSSkill

import os
import json

# Populate settings.json with default values, do so here
DEFAULT_SETTINGS = {
    "memories_data_path": "/home/ovos/NTR-Data/MeePiMemoryBank.json",
    "media_folder": "/home/ovos/MeePi_Media",
}


class VisualRecallSkill(OVOSSkill):
    def __init__(self, *args, **kwargs):
        """ load and pre-process settings and data """
        super().__init__(*args, **kwargs)
        self.learning = True

        # Load settings from self.settings
        self.memories_data_path = self.settings.get("memories_data_path")
        self.media_folder = self.settings.get("media_folder")

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
            self.speak_dialog("error_initialization")
        else:
            self.speak("Memory Palace is Alive - Step 6 - Modern init/super")

    def initialize(self):
        # merge default settings
        # self.settings is a jsondb, which extends the dict class and adds helpers like merge
        self.settings.merge(DEFAULT_SETTINGS, new_only=True)

    @classproperty
    def runtime_requirements(self):
        return RuntimeRequirements(
            internet_before_load=False,
            network_before_load=False,
            gui_before_load=False,
            requires_internet=False,
            requires_network=False,
            requires_gui=False,
            no_internet_fallback=True,
            no_network_fallback=True,
            no_gui_fallback=True,
        )

    @property
    def my_setting(self):
        """Dynamically get the my_setting from the skill settings file.
        If it doesn't exist, return the default value.
        This will reflect live changes to settings.json files (local or from backend)
        """
        return self.settings.get("my_setting", "default_value")

    @intent_handler("ShowMe.intent")
    def handle_show_me_intent(self, message):
        memory_name = message.data.get("memory_name")
        if not memory_name:
            self.speak("I didn't catch the memory you're looking for.")
            return

        folder = self.find_matching_folder(memory_name)
        if not folder:
            self.speak(f"I couldn't find any media for {memory_name}")
            return

        cover_image = os.path.join(folder, "cover.jpg")
        if os.path.exists(cover_image):
            self.gui.show_image(cover_image, fill='PreserveAspectFit')
            self.speak('Here is the cover image from my memory palace')
        else:
            self.speak(f"I found the memory folder, but no image to show for {memory_name}.")

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

    def stop(self):
        """Optional action to take when "stop" is requested by the user.
        This method should return True if it stopped something or
        False (or None) otherwise.
        If not relevant to your skill, feel free to remove.
        """
        return
