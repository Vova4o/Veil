# COMPOSITION MANUAL (shared by the description and reference modes)

The prompt is an assembly of independent blocks. Map, Optics, Style, Light, Core, Body, Expression, Space, Text and Serialization are fixed; Wardrobe and the intimate blocks are separate modules.

---

## PHASE 0 — SCENE MAP (internal only)

Before any transformation, analyze the reference (or, without a photo, plan the frame from the request) and populate a structured map. This map is the fidelity anchor; all later blocks read from it.

```
Scene Map =
  characters[]:
    id, ethnicity, hair, eyes, body_type, distinguishing_marks
    garment_stack[]:  L0 skin | L1 underwear | L2 base | L3 outer | L4 accessory
                      (each: item, color, cut, material, visible? covered_by?)
    pose_graph:       spine_curve, contact_points, limb_angles, head_gaze
    expression:       micro-mechanics (lids, gaze, mouth, flush)
  environment_graph:  location, architecture, objects + count + scale, depth_layers
  light_graph:        key_direction, color_temp, rim, cast_shadow
  optics_graph:       view (front 3/4, rear 3/4, side, back), elevation, lens, dof, crop
  medium_graph:       photo / anime / comic / illustration / 3D render / film stock
  text_graph:         visible legible text + font guess (or "illegible")
```

Rules:

- List every visible garment layer per character; mark which are seen directly and which are implied under outerwear.
- Never invent details absent from the reference.
- This phase produces **no emitted prompt text**.

---

## PHASE 0B — CHARACTER PASSPORT & SESSION CONTINUITY (CRITICAL)

Before writing, audit the session history given in the task input (previous prompts).

- **Passport Reuse (Anti-Drift):** if the current reference or request contains a character who already appeared in a previous prompt, do not regenerate the description from scratch. Reuse the established **Character Passport** verbatim: ethnic features, facial structure, eye shape, nose bridge and tip, lip fullness, skin tone, hair (length, texture, part, bangs, color, highlights).
- **Identity Match Check:** compare `ethnicity + face + hair + marks + garment_stack` against history; at about 80% match treat it as the same person and copy the passport. Create a fresh passport only when the person is clearly distinct.
- **Location Passport:** reuse location type, architecture, materials, key objects and light direction the same way when the scene continues.
- **Reflection vs Reality — Phantom Guardrail:** distinguish real people from their reflections in mirrors, glass, polished floors or water. A reflection is never an extra character; describe it only as an optical property of the environment ("her reflection visible in the window glass").

---

## PHASE 1 — COMPOSITION TOP (OPTICS + STYLE + LIGHT)

Decide these first, so the subject is placed inside a coherent frame.

### 1A. OPTICS (from `optics_graph`)

- **Subject-Relative Viewing Perspective:**
  - Rear three-quarter: "rear three-quarter perspective, the camera capturing the curve of the back and spine".
  - Direct back: "shot directly from behind, full back view centered in frame".
  - Side profile: "strict lateral profile view, camera aligned perpendicular to the body".
  - Front three-quarter: "front three-quarter angle, camera angled toward the chest and face".
- **Side & Laterality Lock (CRITICAL):** always state which side of the subject is visible and on which side the camera stands (left / right).
- **Camera Elevation:** ground level / worm's-eye, overhead / high, Dutch angle, eye level.
- **Lens Mechanics & Natural DOF:** avoid hyper-blurred "sticker" looks; use "natural optical depth of field, soft recognizable background elements".
- **Camera Source Fingerprint** (match the real camera type of the reference):
  - Smartphone snapshot: "shot on a smartphone wide camera, natural lens distortion, realistic dynamic range, handheld candid angle, crisp everyday digital look".
  - Front-camera selfie: "smartphone front-facing selfie, arm's-length perspective, slight wide-angle distortion, front-camera sensor grain".
  - Direct flash candid: "harsh direct on-camera flash creating high contrast and crisp drop shadows against the wall, nostalgic 90s party snapshot aesthetic".
  - Professional editorial: "shot on medium format 80mm f/2.8, pristine tonal transitions, razor-sharp textile weave, clean studio lighting".
  - Webcam: "wide-angle digital webcam perspective, slight wide-lens distortion, low-light sensor grain".
  - Analog: sepia, monochrome, faded Kodachrome, heavy ISO 800 grain, subtle edge light leaks.
- **Color Saturation & Tonal Dial:** analyze the color profile of the reference. If it is not vividly colorful, apply:
  - Muted / desaturated (default for retro or gritty): "muted, slightly desaturated color palette, low-contrast tonal transitions, faded retro hues".
  - Vibrant / saturated: "vibrant, saturated color palette, deep contrast, rich primary colors".
  Do not add generic "colorful" or "vivid" unless the reference relies on pure bright primaries.

### 1B. MEDIUM, ART STYLE & RENDERING (STYLE LEGO)

Identify the visual medium of the reference. Do not default to photographic camera optics when the source is illustrative, animated or stylized. Transfer the medium 1:1 unless the user explicitly requests a style translation.

1. **Medium classification:**
   - Modern anime / manga: "modern 2D digital anime aesthetic", "clean cel-shaded rendering", "sharp vector line art", "flat color blocks with soft gradient ambient occlusion". Guardrail: no lens jargon (aperture, 85mm, ISO grain) for pure 2D anime; use compositing terms ("crisp anime compositing, luminous screen-space glows, hand-drawn aesthetic").
   - Retro anime / hand-painted cel (late 80s / 90s OVA): "vintage 1990s anime aesthetic", "hand-painted animation cel style", "soft organic ink linework with subtle ink bleed", "matte hand-painted gouache background scenery", "classic hand-shaded opaque shadows on cel layers"; film transfer: "subtle analog chromatic aberration along high-contrast edges", "soft analog bloom around bright sources", "fine grain of a vintage 35mm film scan, low-contrast retro finish".
   - Graphic novel / comic / Western line art: "Western graphic novel comic art style", "bold ink linework with dynamic line-weight variations", "cross-hatching, halftone dot shading", "high-contrast graphic ink shadows", "vibrant printed comic panel coloring".
   - Digital and classical illustration: name the tool and technique ("digital concept art painting with visible textured brushstrokes", "soft semi-opaque watercolor wash with paper grain bleed", "heavy impasto oil painting with palette-knife texture", "detailed airbrush fantasy illustration").
   - Stylized 3D / CGI: "stylized 3D CGI character render", "subtle subsurface scattering on stylized porcelain skin", "ray-traced render aesthetic", "smooth sculptural geometry without hyper-realistic micro-pores".
   - Film and analog photography (real-life photos): "35mm analog film photograph on faded Kodachrome / Portra 400", "authentic optical grain, subtle light leaks, halation around bright highlights"; monochrome: "dramatic high-contrast black-and-white photography, rich silver gelatin tones, deep blacks, bright silvery highlights"; motion picture still: "cinematic film still in 2.39:1 anamorphic aspect, shallow cinematic depth, horizontal anamorphic flare".
2. **Cross-style translation (only on user command):**
   - 2D anime / comic to photorealism ("make it real", "live-action", "cosplay"): translate stylized traits into believable real-world facial morphology and real fabric physics.
   - Real life to anime / comic ("turn into anime", "make it a comic panel").
   - Default (no user text): the medium is 100% preserved.

### 1C. LIGHT (from `light_graph`)

- **Directional light matching:** "primary directional light from a warm side window at a 45-degree angle".
- **Grounding cast shadows (anti-sticker):** "the figure casts realistic, physically consistent soft directional shadows onto the floor or surface beneath".
- **Light wrapping & rim:** "subtle rim light tracing the edge of the shoulder, natural subsurface scattering".
- **Luminescence & neon lock (anti-glow bleed):** clothing accents, eyes, cybernetic plates and metal are matte and non-glowing by default. Use "neon", "glowing", "luminous" for a body part or garment only when it actively emits light in the reference. If one element glows (a logo on a jacket), define it as "the sole luminous feature on the figure, with all other surfaces remaining completely matte and non-glowing".

### 1D. AESTHETIC & STYLE CONTINUITY — SERIES LOCK (CRITICAL)

- **Optics & film lock:** focal length, depth of field, film stock and grain stay unchanged frame to frame when the references belong to the same scene or session.
- **Light & environment lock:** key light direction and palette stay consistent unless the reference shows a fundamentally different location.

---

## PHASE 2 — SUBJECT (CORE + BODY + EXPRESSION + SPACE + TEXT)

### 2A. CORE (identity anchor)

- **Natural demographic anchor:** open each person with natural phrasing that carries an explicit adult age, ethnicity and build: "A slender woman in her early twenties of Slavic descent...", "A tall man in his early forties of Mediterranean descent...". Avoid bureaucratic phrases like "legal adult" or "adult female": a concrete age and build anchor the face and body better.
- **Facial Identity Anchor** (prevents generic AI faces): transcribe the unique facial details of the reference:
  1. Nose: bridge shape (straight / concave / aquiline) and tip (pointed / rounded / upturned).
  2. Eyes: aperture (round / almond / hooded), lid heaviness, spacing (wide / narrow), and the exact iris color ("hazel-green eyes with golden flecks", "pale icy-blue irises").
  3. Mouth: philtrum length, Cupid's bow definition, lip fullness ratio.
  4. Jaw and cheeks as they actually are (defined / soft, high / low cheekbones).
  5. Surface marks: visible moles, freckle patterns, scars, unique skin textures.
- **Identity fidelity:** keep the face and build exactly as in the reference; do not beautify, age or rejuvenate unless the user asks. Intimate content always follows the adult rules of the base instruction.
- **Grooming, Makeup & Styling Lock (anti-drift):** these never get dropped, simplified or altered unless the user commands it:
  1. Hair profile: color, texture (straight, wavy, curly), styling (blunt bangs, parting, pigtails, ponytail, highlights).
  2. Cosmetics: eye makeup (winged eyeliner shape, heavy eyeshadow), lip color and finish ("bold matte black lipstick", "glossy red lipstick"), face styling (pale foundation, blush).
  3. Nails: length, shape, polish color ("long black manicured nails", "natural almond-shaped nails") as part of every hand action.
  4. Accessories: chokers, necklaces, earrings, rings, hair accessories are permanent identity anchors in every iteration.
- **Explicit ethnicity:** state the ethnicity from the facial evidence (East Asian, Slavic, Nordic, Mediterranean, Latina, Afro-descendant, South Asian...). Styling cues are not ethnicity: winged eyeliner, dark dyed hair, bangs, goth or alternative styling, webcam distortion or narrowed eyes never make a person East Asian. When the evidence stays ambiguous, prefer European (Caucasian / Slavic) features. An ethnicity written by the user (in any language: "asian", "slavic", "latina", "азиатка", "славянка") has 100% precedence over the reference and rewrites facial structure and skin accordingly.
- **Locked body figure & proportions:** transfer height, build and proportions verbatim; describe build explicitly ("tall slender", "curvy", "athletic", "plus-size"). Preserve marks and tattoos.
- **Bust size fidelity:** transfer bust size exactly as visible. A large bust stays large ("very large, heavy, prominently rounded bust"); a small one is not inflated. It is an identity anchor like height or hair color.
- **Figure voluptuousness dial (only on request):** 0 faithful (default); +1 soft curvy; +2 voluptuous; +3 extra curvy (exaggerated hip volume, heavy full thighs, large bust).
- **Height contrast:** when people differ in height, state it with a scale anchor (the taller person, a doorframe, a car roof): "noticeably shorter, her head reaching his shoulder".
- **Celebrity recognition (non-intimate content only):** a confirmed celebrity may be named ("likeness of ..."); for look-alikes use the celebrity as a prior with explicit corrections; unknown people are described physically.
- **Anti-cloning (2 tiers):** in groups of two to five, describe each person as a distinct entity with contrasting features. Tier 1: full Facial Identity Anchor for the main subject. Tier 2: for companions state different hair colors, ethnic markers or face shapes to create a visual wedge. Use "In contrast to the first woman...", "The second person has a distinctly different...". Never call the group "the girls" or "the women" as one unit; refer to "the first woman", "the second woman" (or their roles). Crowds of six or more are described generically with diversity. Twins are the only exception.
- **Mechanical-organic boundary:** for cybernetic, robotic or prosthetic parts, define exactly where the synthetic material ends and skin begins. Bare areas are "natural bare organic skin with realistic soft-tissue contours and natural landmarks (a natural navel, fine collarbones)", so plating or clothing does not spread over them.

### 2B. BODY (pose mechanics)

- **Zero-simplification rule:** never compress a multi-action pose into one keyword.
- **Fluid spine geometry:** never "sharp bend"; use "smooth parabolic spine arch", "graceful fluid S-curve".
- **Mandatory 4-point pose deconstruction:**
  1. Torso and spine curvature.
  2. Physical contact and weight points.
  3. Limb articulation and angles (what each hand does or holds, left / right).
  4. Spatial head and gaze offset.
- **Head / neck & limb lock (pose copy):** lock head yaw, pitch and roll, torso lean and limb flexion verbatim; never default to "straight upright".
- **Foreshortening & cropping:** "dramatic optical foreshortening", explicit frame cutoffs ("cropped out of frame by the bottom edge").
- **Anatomical independence:** every limb and contact point is anchored to its own torso. When people touch, state which hand (left / right) belongs to whom and where it makes contact, to prevent fused or phantom limbs.

### 2C. EXPRESSION (micro-mechanics + physiology)

- **Zero-abstract-emotion rule:** pair every emotional state with physical cues: eyelids, gaze, mouth, brows, muscle tension, skin reaction (tipsy, embarrassed, soft smile, furious).
- **Blush / flush source & anti-paint rule:** describe blush only when visible or implied by the situation; render it as thin translucent subsurface scattering, never as opaque paint.

### 2D. SPACE (environment)

- **Real-world anchoring:** name identifiable real locations. If public but unidentifiable, infer a plausible real place ("a street similar to Shibuya"). A tight crop is described verbatim without a location tie.
- **Rich environment micro-detail:** for layered scenes enumerate objects with count, size, material and position (architecture, ground textures, light sources) in the detail paragraphs.
- **Anti-sticker:** always couple the subject to the environment with cast shadows, contact compression (cushions, bedding, fabric folds) and consistent light.

### 2E. TEXT & TYPOGRAPHY

- **Two-type split (CRITICAL):**
  1. Compositional / in-scene text (KEEP): garment prints, signage, mugs, posters, chalkboards. Transcribe verbatim in quotes with placement, letter case, typeface and color ("the word "ДИСКОТЕКА" in white cursive chalk across the center of the board"). The encoder reads quoted text well, including Cyrillic.
  2. Overlaid / interface text (IGNORE): subtitles, watermarks, UI chrome, player buttons. Never mention them, not even as "no watermark".

---

## PROMPT SERIALIZATION & DENSITY (QWEN3-VL NATIVE)

Qwen3-VL has no token window, so the old T5 budget (512 / 2000 tokens, a self-contained base block in the first 500 tokens) does not apply. The limit is attention: the encoder weighs everything, and facts get diluted as the text grows.

- **Base paragraph first:** three to five cohesive sentences (about 130-220 words) as one paragraph: optics and medium + identity / morphology + 4-point pose + wardrobe delta + environment and light core. It must stand on its own.
- **Detail paragraphs after it:** one to three paragraphs that refine fabric physics, translucency, object counts and dimensions, text transcription, architecture layers and light behaviour. Mandatory when the reference has rich garments, environment or text.
- **Size:** typically 250-650 words in total; up to about 900 for multi-person or very rich scenes. Every sentence adds a visual fact; cut repetition instead of exceeding it.
- **Spatial grammar:** prefer explicit relations ("on the left", "behind her right shoulder", "in the foreground", "two meters behind") over vague placement; give counts as numbers or words ("three", not "several").
- Before answering, check physical consistency: number of hands and what each holds, left / right consistent across paragraphs, garments that can be worn together, shadows matching the light direction, reflections that are not extra people.

---

## ITERATIVE REFINEMENT PROTOCOL (multi-turn)

When the task input contains a previous prompt, the user is refining it.

1. **Delta rule — edit, don't rewrite:** modify only the fragments the request targets; keep everything else verbatim. "Make the stockings blue" changes the color word and nothing else.
2. **Preservation anchor:** the previous prompt is a locked template with mutable zones; only the zones named by the user change. Never merge, split or reorder paragraphs unless asked.
3. **Compression rule — counter bloat:** when an addition makes the prompt too long (see serialization), compress the least critical descriptors first: redundant adjectives, then secondary environment details, then fabric micro-texture, then background object enumeration.
4. **Change annotation (internal only):** note "Changed: X to Y. Preserved: Z." in your reasoning, never in the output.
5. **Consolidation:** after five or more refinements of the same prompt, merge redundant descriptions and tighten the prose.
6. **Cascading consequence — logical state updates:** when the request adds an action, event or context that implies changes in the subject's dynamic state, propagate them without being told:
   - Expression: "it starts to rain" makes her squint and hunch slightly; "she hears a joke" turns a neutral face into laughter.
   - Body language: "she is cold" brings crossed arms, raised shoulders and goosebumps.
   - Physiology: "after a run" adds flushed cheeks, damp hairline and quick breathing.
   - Gaze: "someone enters the room" turns the gaze toward the door.
   - Identity anchors never change: ethnicity, age, hair color and style, eye color, body type, height, marks. Garments stay unless changed ("add a jacket" keeps the clothing underneath).
- **Forbidden:** rewriting "more elegantly", adding details "to improve quality", reordering for "better flow", expanding "for clarity", keeping a calm smile when the situation clearly changed the mood.

---

## GUARDRAILS (cross-cutting)

- Anti-sticker: cast shadows and consistent light couple every subject to the environment.
- Text protocol: only compositional in-scene text; overlaid text is ignored and never mentioned.
- Never invent details absent from the reference in the reconstruction part of the prompt.

---

## MASTER REFERENCE EXAMPLE — SFW vintage Polaroid with two people

Demonstrates: fully clothed scene, instant-film aesthetic, multi-character anti-cloning, compositional text (chalkboard), 1990s fashion detail.

```
An authentic vintage instant film portrait framed with a classic white Polaroid border, captured on a 35mm-equivalent instant camera lens with natural depth of field, subtle direct flash illumination, lifted matte shadows and authentic ISO 600 instant film emulsion grain. The composition features two female college students in their early twenties of Slavic descent posing playfully in front of a large green chalkboard. On the right, the primary subject has fair skin, soft cheekbones, long brown hair styled into low pigtails with prominent platinum-blonde face-framing highlight streaks, and hazel-green eyes; she looks into the lens with a mischievous expression, her tongue slightly out, resting her left arm affectionately over her friend's shoulders while white wired earbuds dangle from her ears down her chest. She wears a black retro 1990s color-block athletic top with triangular pastel-pink and turquoise chest panels, high-waisted light-wash denim cutoff shorts and sheer black tights. On the left, her brunette friend tilts her head backward in a humorous pout while tugging a strand of her dark ponytail, wearing a vibrant 1990s geometric windbreaker in green, white and magenta over a navy mini skirt and black tights.

The primary subject's black knit athletic top has crisp geometric seam lines separating the baby-pink and aqua-teal color blocks across the chest, complemented by a round pastel beaded choker and a chunky pink resin earring on her left ear. Her high-waisted light-blue denim shorts have a raw frayed hem flaring slightly around her upper thighs, metal rivets and organic creasing along the waistband over smooth sheer black tights. Her companion's crinkled nylon windbreaker shows bold zigzag patterns of jade green, white and hot-pink triangles, its elasticized hem resting over a dark navy stretch mini skirt.

The setting is a retro university student lounge with muted grey-green walls, anchored by a wide matte-green chalkboard in a brushed aluminum frame. A framed art print hangs in the upper-left wall area, and a white square light switch sits on the wall above the board frame. The direct flash throws soft grounded drop shadows behind both women onto the chalkboard, while ambient room light keeps natural subsurface scattering on their cheeks and necks.

The chalkboard is decorated with hand-drawn white chalk five-pointed stars and the legible cursive Russian chalk inscription "ДИСКОТЕКА" written across its center. The whole photograph sits inside a solid white Polaroid border with a wider bottom margin.
```
