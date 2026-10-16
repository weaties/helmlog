// Drag-to-pan for 360° YouTube videos on the session page (#844).
//
// YouTube's embedded player doesn't respond to mouse drags from a third-party
// page, but the IFrame API's get/setSphericalProperties() do work. A
// transparent layer over the iframe turns drags into setSphericalProperties()
// calls. The layer is only shown when the current video reports spherical
// properties, so flat videos behave exactly as before.

const _PAN_CLICK_SLOP_PX = 5;

// The scene follows the pointer ("grab the scene", as on youtube.com and
// Street View): drag right → scene slides right. In YouTube's convention that
// is increasing yaw (+yaw turns the view left) and, for a downward drag,
// increasing pitch (+pitch looks up). Dragging across the full layer width
// rotates by one field of view.
function panFromDrag(start, dx, dy, widthPx) {
  const k = widthPx > 0 ? start.fov / widthPx : 0;
  const yaw = (((start.yaw + dx * k) % 360) + 360) % 360;
  const pitch = Math.max(-90, Math.min(90, start.pitch + dy * k));
  return { yaw: yaw, pitch: pitch, roll: start.roll, fov: start.fov };
}

// YT returns {} for flat videos and {yaw, pitch, roll, fov} for 360 ones.
function isSpherical(props) {
  return !!props && typeof props.yaw === 'number';
}

function isClick(dx, dy) {
  return Math.hypot(dx, dy) < _PAN_CLICK_SLOP_PX;
}

// Wire the pan layer to a YT player. getPlayer() is called lazily so the
// caller can swap players. Returns {refresh}, which shows/hides the layer
// based on whether the current video is spherical — call it on player ready
// and on state changes (video switches go through loadVideoById → state
// changes).
function attachVideoPan(layer, getPlayer) {
  let drag = null;

  function props() {
    const p = getPlayer();
    if (!p || typeof p.getSphericalProperties !== 'function') return null;
    try { return p.getSphericalProperties(); } catch (e) { return null; }
  }

  layer.addEventListener('pointerdown', (e) => {
    const start = props();
    if (!isSpherical(start)) return;
    layer.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, start: start, moved: false };
    layer.style.cursor = 'grabbing';
  });

  layer.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (!drag.moved && isClick(dx, dy)) return;
    drag.moved = true;
    getPlayer().setSphericalProperties(panFromDrag(drag.start, dx, dy, layer.clientWidth));
  });

  function endDrag() {
    if (!drag) return;
    const wasClick = !drag.moved;
    drag = null;
    layer.style.cursor = 'grab';
    // The layer swallows YouTube's own click-to-play, so a click without a
    // drag toggles playback instead. YT.PlayerState.PLAYING = 1.
    if (wasClick) {
      const p = getPlayer();
      if (p.getPlayerState() === 1) p.pauseVideo(); else p.playVideo();
    }
  }
  layer.addEventListener('pointerup', endDrag);
  layer.addEventListener('pointercancel', () => { drag = null; layer.style.cursor = 'grab'; });

  return {
    refresh: function() {
      layer.style.display = isSpherical(props()) ? 'block' : 'none';
    },
  };
}

if (typeof module === 'object' && module.exports) {
  module.exports = { panFromDrag: panFromDrag, isSpherical: isSpherical, isClick: isClick, attachVideoPan: attachVideoPan };
}
