(function () {
  "use strict";

  function findCue(cues, milliseconds) {
    let low = 0;
    let high = cues.length - 1;
    let found = -1;
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      if (cues[middle].start_ms <= milliseconds) {
        found = middle;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    if (found < 0) return -1;
    const cue = cues[found];
    return milliseconds < cue.end_ms ? found : -1;
  }

  function mount(audio, cues, cueElements) {
    function sync() {
      const active = findCue(cues, audio.currentTime * 1000);
      cueElements.forEach((element, index) => {
        element.classList.toggle("active", index === active);
      });
    }
    audio.addEventListener("timeupdate", sync);
    audio.addEventListener("seeked", sync);
    sync();
    return {
      sync,
      destroy() {
        audio.removeEventListener("timeupdate", sync);
        audio.removeEventListener("seeked", sync);
      },
    };
  }

  window.PodcastPlayer = { mount, findCue };
})();
