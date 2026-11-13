import QtQuick 2.12
import QtMultimedia 5.12
import Mycroft 1.0 as Mycroft

Mycroft.Delegate {
    Rectangle { anchors.fill: parent; color: "black" }
    Video {
        id: player
        anchors.fill: parent
        source: sessionData.videoSource
        autoPlay: true
        fillMode: VideoOutput.PreserveAspectFit
        onStatusChanged: {
            if (status === MediaPlayer.EndOfMedia) {
                triggerGuiEvent("visual.recall.video.done", {})
            }
        }
        onErrorChanged: {
            if (error !== MediaPlayer.NoError) {
                console.log("VideoPlayer error: " + errorString)
                triggerGuiEvent("visual.recall.video.done", {})
            }
        }
    }
}