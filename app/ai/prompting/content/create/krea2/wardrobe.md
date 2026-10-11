# PHASE 3 — WARDROBE LEGO (surgical garment operations)

Wardrobe is a **delta** applied to the `garment_stack` of the Scene Map. Every operation is reversible and touches only the named layer.

- `KEEP_EXACT(layer)` — reproduce the item 1:1.
- `REMOVE_SELECTIVE(layer)` — take off only that layer; outer layers stay identical (remove the scarf, keep the open coat).
- `MODIFY_CUT(layer)` — shorten a hem or add a cut, anchored to anatomical landmarks.
- `SHEERIFY(layer)` — true optical translucency; contours show through the weave.
- `SHIFT_ASIDE(layer)` — push a fabric edge aside, roll a sleeve up, slip a strap off the shoulder.
- `ADD(layer)` / `REPLACE(layer)` — a new garment with full fidelity parameters (below).

## Wardrobe fidelity, fabrics & color lock (anti-drift)

Never describe garments with generic words ("black dress", "shirt"). Lock these parameters 1:1 from the reference, or decide them explicitly for new items:

1. **Fabric & material physics:** textile type, weight and weave ("sheer 20-denier nylon mesh", "matte heavy-weight cotton", "ribbed knit spandex", "glossy lambskin leather", "chunky cream cable knit").
2. **Color precision:** exact shades and transparency ("matte obsidian black", "charcoal grey", "vibrant crimson", "translucent pale pink").
3. **Structural cut & seams:** cuts anchored to anatomy ("high-necked halter", "low scoop neckline", "steeply curved high-leg hip cuts", "ribbed side seams", "hem ending at mid-thigh").
4. **Layer integrity (anti-compression):** a layer described as "sheer black floral lace bell sleeves" stays exactly that in every later turn unless the user changes it.

**Apparel cut & fabric physics:** use anatomical anchors for hems and cuts; show physical fabric tension, drape, creasing and compression where the garment meets the body or furniture. Never use detached graphic overlays for anatomy.

**Male wardrobe lock:** male-presenting characters keep their wardrobe 1:1; exposure, sheer and cut operations apply only when the user explicitly asks for them on that man.

**Phones and cameras in frame:** name only real models (a current iPhone, a high-end Galaxy) or describe them generically.
