// blackhole.glsl — a floating black hole for Ghostty
//
// Inspired by Eric Bruneton's "Real-time High-Quality Rendering of
// Non-Rotating Black Holes" (https://ebruneton.github.io/black_hole_shader/).
// His implementation beam-traces Schwarzschild geodesics against precomputed
// lookup tables; a Ghostty custom shader is a single screen-space pass, so the
// same visual ingredients are approximated here instead:
//
//   * gravitational lensing  — weak-field deflection (Einstein-ring mapping)
//     applied to the terminal texture, so text bends, magnifies and flips
//     around the hole
//   * event horizon          — pure shadow disc
//   * photon ring            — thin bright ring just outside the horizon
//   * accretion disk         — Keplerian streaks, doppler beaming (approaching
//     side blue-white and bright, receding side dim orange-red), plus a faint
//     circular "lensed image" of the disk's far side
//   * signal degradation     — as the hole grows the terminal "feed" breaks
//     down old-tv style: defocus blur, grainy static snow, scanlines,
//     vignette/flicker and a rolling band. It shares the hole's exact grow and
//     idle-fade timing and its WORK_AREA exemption (tuned via CRT_* below).
//
// Ghostty setup (~/.config/ghostty/config):
//   custom-shader = /path/to/blackhole_ghostty/blackhole.glsl
//   custom-shader-animation = true

// ---------------------------------------------------------------- tunables --
const float HOLE_RADIUS   = 0.0850; // event horizon at FULL intensity (fraction of screen height)
const float LENS_STRENGTH = 0.2600;  // Einstein radius at full intensity — how far text bends
const float DISK_GAIN     = 1.0000;  // accretion disk brightness
const float DRIFT_SPEED   = 1.0000;   // how fast the hole floats around
const float DISK_TILT     = 0.5000; // disk tilt, radians
const float WORK_AREA     = 0.0000; // bottom screen fraction kept undistorted
const float DILATION_MIN  = 0.1000; // animation time rate when the hole is fully grown (gravitational time dilation)

// ----------------------------------------- signal degradation (old-tv CRT) --
// A grainy breakdown that grows in lockstep with the hole: it scales by the
// same intensity I, fades on the same idle pause, and respects the same
// WORK_AREA band as the lens warp. By full mass the terminal is defocused,
// snowed over, scanlined and dimmed at the edges — barely legible — then a
// desynced bright band rolls down it. All zero out until the hole appears.
const float CRT_BLUR       = 0.0110; // defocus radius at full mass (fraction of screen height)
const float CRT_STATIC     = 0.5500; // static-snow mix at full mass (1.0 = pure snow)
const float CRT_SCANLINE   = 0.1800; // scanline darkening depth
const float CRT_VIGNETTE   = 0.6000; // corner darkening at full mass
const float CRT_FLICKER    = 0.0600; // global brightness flicker amount
const float CRT_ROLL       = 0.1200; // rolling-band brightness
const float CRT_ROLL_SPEED = 0.0800; // rolling-band scroll rate (screens/sec)

// --------------------------------------------------- work session, overlay-fed --
// "Reset after a break" needs memory, which a stateless shader doesn't have,
// so the overlay (overlay.py) tracks the session and feeds it in as
// iWorkSeconds: continuous seconds of work, zeroed once a typing pause
// reaches IDLE_RESET_MIN. The hole stays invisible until GROW_AFTER_MIN of
// work, then ramps to full size over GROW_RAMP_MIN and stays until the next
// reset. A pause fades it out smoothly, hitting invisible exactly when the
// pause becomes a reset — after which the 4-hour clock starts over.
const float GROW_AFTER_MIN = 180.0000; // continuous work before the hole appears
const float GROW_RAMP_MIN  = 5.0000; // growth ramp once it appears
const float IDLE_RESET_MIN = 10.0000; // typing pause that resets the session
const float IDLE_FADE_SEC  = 60.0000; // fade-out length, ending at the reset
const float TIME_SCALE     = 1.0000; // TESTING: >1 fast-forwards the session (e.g. 100 -> 4 h in ~2.4 min). Set back to 1 for normal use.

// ------------------------------------------------------------------- noise --
float hash21(vec2 p) {
    p = fract(p * vec2(234.34, 435.345));
    p += dot(p, p + 34.23);
    return fract(p.x * p.y);
}

float vnoise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash21(i),               hash21(i + vec2(1.0, 0.0)), f.x),
               mix(hash21(i + vec2(0.0, 1.0)), hash21(i + vec2(1.0, 1.0)), f.x),
               f.y);
}

// mirrored repeat keeps lensed samples on-screen without edge smearing
vec2 mirrorUV(vec2 u) { return 1.0 - abs(1.0 - mod(u, 2.0)); }

// 12-tap unit disk (two rings) for the CRT defocus blur
const vec2 BLUR_DISK[12] = vec2[12](
    vec2( 0.0000,  0.5000), vec2( 0.4330,  0.2500), vec2( 0.4330, -0.2500),
    vec2( 0.0000, -0.5000), vec2(-0.4330, -0.2500), vec2(-0.4330,  0.2500),
    vec2( 0.0000,  1.0000), vec2( 0.8660,  0.5000), vec2( 0.8660, -0.5000),
    vec2( 0.0000, -1.0000), vec2(-0.8660, -0.5000), vec2(-0.8660,  0.5000)
);

// per-pixel, per-frame static snow; the time seed is wrapped so it stays
// precise (and the snow keeps churning) even hours into a session
float staticNoise(vec2 fc, float t) {
    float fseed = mod(floor(t * 60.0), 1024.0);
    return hash21(fc + vec2(fseed * 1.7, fseed * 3.1));
}

vec2 rot(vec2 v, float a) {
    float c = cos(a), s = sin(a);
    return vec2(c * v.x - s * v.y, s * v.x + c * v.y);
}

// doppler beaming: q.x < 0 side orbits toward the viewer
float beaming(vec2 q, float r) {
    return smoothstep(1.0, -1.0, q.x / max(r, 1e-5));
}

// blackbody-ish disk palette: receding/outer = orange-red, approaching/inner = blue-white
vec3 diskPalette(float heat) {
    vec3 cool = vec3(1.00, 0.38, 0.08);
    vec3 mid  = vec3(1.00, 0.80, 0.45);
    vec3 hot  = vec3(0.85, 0.90, 1.00);
    return heat < 0.5 ? mix(cool, mid, heat * 2.0) : mix(mid, hot, heat * 2.0 - 1.0);
}

// ------------------------------------------------------------------- image --
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2  res    = iResolution.xy;
    vec2  uv     = fragCoord / res;
    float aspect = res.x / res.y;

    // Ghostty's fragCoord y runs top-down; work in height-from-bottom
    float yUp = 1.0 - uv.y;

    // ---- work-session state, fed by the overlay ----
    // invisible until GROW_AFTER_MIN of continuous work, then a quick ramp
    // to full intensity over GROW_RAMP_MIN
    float work = iWorkSeconds * TIME_SCALE;
    float I = clamp((work - GROW_AFTER_MIN * 60.0) / (GROW_RAMP_MIN * 60.0),
                    0.0, 1.0);
    // typing detector: a pause fades the hole out, reaching invisible exactly
    // when the pause becomes a session reset (the overlay then zeroes
    // iWorkSeconds, so it stays gone until the next 4 hours are up)
    float idle = max(0.0, iTime - iTimeCursorChange);
    float resetSec = IDLE_RESET_MIN * 60.0;
    I *= 1.0 - smoothstep(max(resetSec - IDLE_FADE_SEC, 0.0), resetSec, idle);
    float vis = smoothstep(0.0, 0.10, I);  // hole vanishes entirely when rested
    if (vis <= 0.0) {
        fragColor = texture(iChannel0, uv);
        return;
    }
    float sz     = mix(0.22, 1.0, I);      // pops in small, swells through the ramp
    float rh     = HOLE_RADIUS * sz;
    float thetaE = LENS_STRENGTH * sz;

    // CRT signal-degradation envelope: same intensity (I) and visibility gate
    // (vis) as the hole, faded toward the work area exactly like the lens warp
    // below. Every CRT effect scales by this one master amount, so the whole
    // breakdown appears, grows and idle-fades on the hole's timing.
    float crtMask = smoothstep(WORK_AREA, WORK_AREA + 0.18, yUp);
    float crtAmt  = I * vis * crtMask;
    float crtBlur = CRT_BLUR * crtAmt;

    // smooth animation runs off iTime (advances every frame); the wall clock
    // above only drives the slow pomodoro envelope
    float t = iTime * DRIFT_SPEED;

    // ---- gravitational time dilation ----
    // A heavier hole slows the clock locally: the accretion disk visibly winds
    // down as the hole grows. dil multiplies the disk's orbital rate, falling
    // from 1 toward DILATION_MIN as the hole reaches full mass. (Applied to the
    // disk swirl below, not to the drift — scaling the drift frequency by a
    // slowly-varying mass over an unbounded iTime would eventually stall it.)
    float dil = mix(1.0, DILATION_MIN, I);

    // lazy Lissajous drift, vertically confined so the hole and its disk
    // stay above the work area at the bottom of the screen; bounds adapt to
    // the current size (disk half-extent ~2.4*rh after the tilt rotation)
    float ext = 2.4 * rh;
    float yLo = WORK_AREA + 0.12 + ext;      // clears shield band + wobble
    float yHi = max(yLo, 0.90 - ext);        // clears the screen top
    // drift follows size: a small calm hole hovers near its spot, a big one
    // roams wide and fast (amplitude scaling, not frequency — intensity
    // varies over time and frequency modulation would jerk the phase)
    float spd = mix(0.35, 1.0, I);
    vec2 center = vec2(
        0.5 + (0.24 * sin(t * 0.21) + 0.05 * sin(t * 0.083)) * spd,
        1.0 - mix(yLo, yHi, 0.5 + (0.42 * sin(t * 0.157 + 2.0) + 0.08 * sin(t * 0.117)) * spd));
    // restlessness: extra wobble grows with intensity; frequencies stay
    // constant so the position is continuous as intensity evolves
    center += I * vec2(0.040 * sin(t * 0.83) + 0.020 * sin(t * 1.31),
                       0.030 * sin(t * 1.03 + 1.0));

    // aspect-corrected frame centered on the hole (y in units of screen height)
    vec2  p  = (uv - center) * vec2(aspect, 1.0);
    float r  = length(p);

    // ---- gravitational lensing of the terminal contents ----
    // weak-field deflection alpha = thetaE^2 / r, windowed so the far field
    // stays readable; inside the Einstein radius the mapping flips, producing
    // the mirrored secondary image a real lens makes
    float defl   = (thetaE * thetaE / max(r, 1e-4)) * exp(-r * r * 4.0);
    // fade the warp field itself to zero toward the work area — a continuous
    // displacement leaves no visible seam, unlike blending warped/unwarped colors
    defl *= vis * smoothstep(WORK_AREA, WORK_AREA + 0.18, yUp);
    vec2  dir    = p / max(r, 1e-5);
    vec3  term;
    // mild chromatic aberration: blue bends a touch more than red
    for (int i = 0; i < 3; i++) {
        float k   = 1.0 + (float(i) - 1.0) * 0.035;
        vec2  sp  = p - dir * defl * k;
        vec2  suv = mirrorUV(center + sp / vec2(aspect, 1.0));
        term[i]   = texture(iChannel0, suv)[i];
    }

    // ---- CRT defocus: the terminal goes soft as the hole grows ----
    // blur the lensed terminal with a 12-tap disk around the (green) sample,
    // averaged with the sharp center; collapses back to sharp when crtBlur is 0
    if (crtBlur > 0.0) {
        vec2 base = p - dir * defl;            // green-channel lensed position
        vec3 blur = term;                      // include the sharp center tap
        for (int j = 0; j < 12; j++) {
            vec2 suv = mirrorUV(center + (base + BLUR_DISK[j] * crtBlur)
                                / vec2(aspect, 1.0));
            blur += texture(iChannel0, suv).rgb;
        }
        term = blur / 13.0;
    }

    // ---- static snow: grayscale grain washes over the terminal feed ----
    // applied before the shadow mask, so the hole still eats the snow near it
    term = mix(term, vec3(staticNoise(fragCoord, iTime)), CRT_STATIC * crtAmt);

    // shadow: hard black inside the horizon, text fades as it falls in
    float shadow = smoothstep(rh, rh * 1.03, r);
    term *= shadow * smoothstep(rh, rh * 1.5, r);

    vec3 col = term;

    // ---- accretion disk (tilted, flattened ellipse) ----
    vec2  pd = rot(p, DISK_TILT);
    vec2  q  = vec2(pd.x, pd.y / 0.30);        // squash -> disk seen near edge-on
    float rd = length(q);
    float rin  = rh * 1.45;
    float rout = rh * 4.30;

    float band = smoothstep(rin, rin * 1.30, rd) *
                 (1.0 - smoothstep(rout * 0.55, rout, rd));
    if (band > 0.001) {
        float ang = atan(q.y, q.x);
        float kep = pow(rin / rd, 1.5);        // Keplerian: inner orbits faster
        // gravitational time dilation: clocks slow near the horizon, so the
        // inner orbits appear to freeze (Schwarzschild-ish redshift), and the
        // whole disk winds down via dil as the hole grows
        float redshift = sqrt(clamp(1.0 - rh / rd, 0.04, 1.0));
        float swirlA = ang + rd * 22.0 - t * kep * 2.6 * redshift * dil;
        float streaks = vnoise(vec2(rd * 70.0, swirlA * 3.0)) * 0.65 +
                        vnoise(vec2(rd * 24.0, swirlA * 1.5 + 7.0)) * 0.35;
        streaks = 0.35 + 0.9 * streaks * streaks;

        float dop  = beaming(q, rd);                       // 0 receding, 1 approaching
        float emit = pow(rin / rd, 2.2);                   // hotter toward the inner edge
        float heat = clamp(0.85 * dop + 0.45 * (rin / rd) - 0.15, 0.0, 1.0);
        float gain = mix(0.18, 2.4, dop * dop);            // relativistic beaming

        // the half nearer the viewer (lower on screen, +y in this top-down
        // frame) passes in front of the shadow
        float front = smoothstep(-0.004, 0.004, pd.y);
        float occl  = mix(shadow, 1.0, front);

        col += diskPalette(heat) * (DISK_GAIN * band * streaks * emit * gain * occl) * vis;
    }

    // ---- lensed image of the disk's far side: a faint circular halo ----
    float halo = exp(-pow((r - rh * 1.75) / (rh * 0.55), 2.0));
    float hdop = beaming(rot(p, DISK_TILT), r);
    col += diskPalette(0.45 + 0.4 * hdop) * halo * mix(0.06, 0.55, hdop) * shadow * vis;

    // ---- photon ring: thin, hot, just outside the horizon ----
    float ring = exp(-pow((r - rh * 1.16) / (rh * 0.10), 2.0));
    col += vec3(1.0, 0.88, 0.70) * ring * 1.4 * shadow * vis;

    // faint warm ambient glow so the hole reads as an object, not a cutout
    col += vec3(1.0, 0.55, 0.25) * 0.030 * exp(-pow(r / (rh * 3.5), 2.0)) * shadow * vis;

    // ---- CRT post: scanlines, vignette, flicker, rolling band ----
    // applied to the whole composited image (hole included) so it reads as one
    // failing signal; all of it scales by crtAmt, so it shares the hole's
    // timing and stays clear of the WORK_AREA band
    if (crtAmt > 0.0) {
        // fine horizontal scanlines
        float scan  = 1.0 - CRT_SCANLINE * crtAmt
                      * (0.5 + 0.5 * sin(fragCoord.y * 3.14159265));
        // vignette: corners close in
        vec2  vc    = uv - 0.5;
        float vig   = 1.0 - CRT_VIGNETTE * crtAmt * dot(vc, vc) * 3.0;
        // slow global brightness wobble
        float flick = 1.0 + CRT_FLICKER * crtAmt
                      * (vnoise(vec2(iTime * 7.0, 0.0)) - 0.5) * 2.0;
        col *= scan * clamp(vig, 0.0, 1.0) * flick;
        // desynced bright band scrolling down the screen
        float band  = exp(-pow((fract(uv.y + iTime * CRT_ROLL_SPEED) - 0.5)
                               / 0.05, 2.0));
        col += band * CRT_ROLL * crtAmt;
    }

    fragColor = vec4(col, 1.0);
}
