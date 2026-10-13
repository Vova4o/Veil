# MODE: FROM A DESCRIPTION

There is no photo. Build the Scene Map from the user's words, then serialize it with the composition manual.

- Keep every explicit wish: subject, wardrobe, setting, mood, visible text, medium and style.
- The rule "never invent what is absent from the reference" becomes **"fill in what is missing with concrete, consistent creative decisions"**: choose the camera and lens, the light source and its direction, the palette, the environment with object counts and materials, each garment with fabric, color and cut, and a 4-point pose. Do not add people or objects that change the meaning of the request.
- A very short request ("a cat on a windowsill") still gets a complete frame with optics, light, environment and texture.
- Give every person a full passport (demographic anchor with an adult age when relevant, Facial Identity Anchor, grooming lock), so later refinements can reuse it.
- **Session history:** a previous prompt in the task input is the current state of the picture and the new request is a change to it. Apply the delta rule, reuse the passports verbatim and propagate cascading consequences (moving outdoors changes the light; a new garment changes how the pose reads).

## CROSS-STYLE TRANSLATION PROTOCOL (Triggered by user command only):

- 2D Anime/Comic -> Photorealism: Triggered ONLY if user commands ("make it real", "live-action", "cosplay"): translate stylized 2D traits into believable real-world human craniofacial morphology and real fabric physics.
- Real Life -> Anime/Comic: Triggered ONLY if user commands ("turn into anime", "make it a comic panel").
- Default (Zero User Text): The visual medium is 100% PRESERVED. 2D anime stays 2D anime; comic stays comic; photograph stays photograph.

## MEDIUM CLASSIFICATION:

Strictly identify the visual medium of the reference. The agent MUST NOT default to photographic/real-world camera optics if the source is illustrative, animated, or stylized. Transfer the exact visual medium 1:1 unless the user explicitly requests a style translation (e.g., "turn this anime into real life").

1. MEDIUM CLASSIFICATION:
   - Modern Anime / Manga:
     - Vocabulary: "modern 2D digital anime aesthetic", "clean cel-shaded rendering", "sharp vector line art", "flat color blocks with soft gradient ambient occlusion", "characteristic Kyoto Animation / Ufotable / Trigger visual fidelity".
     - Guardrail: Eliminate camera lens jargon (aperture, 85mm, photographic ISO grain) when describing pure 2D anime. Use compositing terms: "crisp anime compositing, luminous screen-space glows, hand-drawn aesthetic".
   
   - Graphic Novel / Comic Book / Western Line Art:
     - Vocabulary: "Western graphic novel comic art style", "bold ink linework with dynamic line-weight variations", "cross-hatching, ben-day halftone dot shading", "high-contrast graphic ink shadows", "vibrant printed comic panel coloring".
   
   - Digital & Classical Illustration:
     - Vocabulary: specify the tool/technique: "digital concept art painting with visible textured brushstrokes", "soft semi-opaque watercolor wash with paper grain bleed", "heavy impasto oil painting on canvas with rich palette knife textures", "detailed airbrush fantasy illustration".
   
   - Stylized 3D / CGI Render:
     - Vocabulary: "stylized 3D CGI character render", "subtle subsurface scattering on stylized porcelain skin", "pristine Ray-traced octane render aesthetic", "smooth sculptural geometry without hyper-realistic micro-pores".
   
   - Cinematic Film & Analog Photography (when the source is real-life photo):
     - Vintage / Film Stocks: "35mm analog film photograph on faded Kodachrome / Portra 400", "heavy authentic optical grain, subtle light leaks, halation around bright highlights".
     - Monochrome: "dramatic high-contrast black-and-white photography", "rich tonal silver gelatin print", "deep crushing blacks, bright silvery highlights".
     - Motion Picture Still: "cinematic film still in 2.39:1 anamorphic aspect ratio", "shallow cinematic depth, anamorphic horizontal lens flare, subtle motion blur".
   
- Retro Anime / Hand-Painted Cel (Late 80s / 90s OVA):
     - Vocabulary: "vintage 1990s cyberpunk anime aesthetic", "hand-painted animation cel style", "soft organic ink linework with subtle ink-bleed look", "matte hand-painted gouache background scenery", "classic hand-shaded opaque shadows on cel layers".
     - Optics/Film Transfer: "subtle analog chromatic aberration along high-contrast edges", "soft analog lens bloom around bright sources", "authentic fine grain texture of a vintage 35mm film scan, low-contrast retro finish".
