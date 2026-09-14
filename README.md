# Frost for Blender

Frost is the path tracer inside FrioStudio: hardware ray tracing on Apple
silicon, physically based from the sky down, and measured against Cycles
on the same Mac at 1.5x the speed per sample and 1.7x to the same noise,
averaged over three scenes. This add-on makes it a render engine in
Blender: choose **Frost** under Render Properties, press F12, and the
frame comes back into Blender's render window.

## What you need

- A Mac with Apple silicon (M1 or later) and macOS 13 or later. Frost runs
  on the GPU's ray tracing hardware and on nothing else.
- Blender 4.2 or later.
- The `frost` executable. It ships inside this add-on (`frost_blender/bin/frost`),
  signed and notarised, and it is also inside every copy of FrioStudio.

## Installing

1. Download `Frost-for-Blender-<version>.zip` from the Releases page.
2. In Blender: Edit > Preferences > Add-ons > the drop-down arrow > Install
   from Disk, and choose the zip. Enable **Frost Render Engine**.
3. Render Properties > Render Engine > **Frost**.

The add-on's preferences show which `frost` it found. Leave the path empty
to use the copy inside the add-on; point it at another to use that one.

If a render shows nothing, the reason is in the render window's header
and in `~/Library/Logs/Frost for Blender/last-render.log`; the
preferences have a button that opens it, and a **Repair** button that
makes the bundled `frost` runnable again (Blender's installer keeps no
permission bits when it unpacks a zip; the add-on repairs that on its
own the first time it renders).

## What it renders

- Every mesh (and curve, text and metaball, evaluated) with its modifiers
  applied, instances included. A scene of a few million triangles is
  handed over in a few seconds; copies of one mesh are sent once.
- Materials through the Principled BSDF: base colour, metallic, roughness
  (as values or image textures), normal maps, emission with its strength,
  and transmission with its IOR. Files imported from glTF come through
  exactly, factors and packed maps included.
- Lights: one sun, point lights, spot lights, and area lights (rendered as
  the emissive surfaces they are, at the power you set).
- The world, in its three forms: a colour at a strength; an **HDRI** (an
  Environment Texture, turned by a Mapping node's Z rotation), which lights
  the scene and stands behind it, with the sun in the picture found by
  Frost's light sampling so it casts real shadows; and the **Sky Texture**
  (Nishita), which becomes Frost's own physical atmosphere with the sun
  where the node puts it, the air by the node's aerosol, the ground's
  altitude, and the strengths calibrated against Cycles on the same scene.
- **Boxes of fog**: a mesh whose material is a Principled Volume (or a
  Volume Scatter) and no surface becomes a volume in Frost -- an even fog in
  the box at the node's density, colour and anisotropy -- rather than a wall.
- The camera: focal length, sensor, clipping, and depth of field when it is
  on.

Frost's own rows are in Render Properties: samples, bounces, denoise,
adaptive sampling with its noise threshold, filter glossy, and exposure.

## Baking what Frost cannot read

Frost reads the Principled BSDF and the pictures wired into it by the
mesh's UVs -- and now, for what it is nearest to, a Diffuse, Glossy or
Metallic, Emission, Glass, Translucent or Transparent shader, and a Mix or
Add of those, so a material without a Principled still comes through with
its colour, its shine and its glow. What no exporter can hand over as it
is: a procedural graph (noise, ramps, mixes by pointiness or vertex
colour) and a picture mapped by Generated or Object coordinates rather
than UVs. The Frost panel counts the objects whose materials are like
that, and **Bake Materials for Frost** bakes them the way every pipeline
that hands a Blender scene to another renderer does: Cycles bakes each
such material's colour, roughness, normal and emission into pictures at
the size you choose (1024 by default), on a UV map made for it by Smart
UV Project, once per object -- a bake is of the material on that object,
since Generated coordinates depend on the object's bounds -- and Frost
renders with the pictures in the material's place. The pictures live in
`~/Library/Caches/Frost for Blender/bakes/<file>/`, the object remembers
them in a custom property, and **Forget the Bakes** takes that off. Bake
again after changing a baked material; the count in the panel says when
nothing needs it. Metallic is taken as the material's value, since Cycles
has no bake for it.

## The traced viewport

Set the viewport's shading to **Rendered** and Frost traces it live, the
way Frio's own viewport does: the picture clears as you watch, starts
again as you orbit, and denoises as it goes. **Viewport Scale** in the
Frost panel sets its resolution as a fraction of the region; a half is the
default and answers a move four times sooner than full size. Orthographic
views are not traced yet. The final render fills in bucket by bucket in
the render window as it goes.

## What it does not do yet

- Orthographic viewports, and the camera frame's borders in camera view
  (the traced view fills the region).
- Animation and motion blur through the add-on. Frost itself does both;
  the add-on renders the current frame.
- Smoke and fire simulations, hair, and shader node trees beyond the
  Principled BSDF and image textures: a procedural material (noise, ramps,
  mixes) renders with the values on its Principled sockets, and a world
  built from nodes other than a colour, an Environment Texture or a Sky
  Texture renders under a plain grey and says so in the log.
- Passes other than Combined.

## The view transform

Frost delivers a finished, tonemapped frame. Blender's **Standard** view
transform shows it as rendered; AgX or Filmic would tonemap it a second
time. The Frost panel offers a one-click switch.

## Licensing

The Python add-on (everything under `frost_blender/` except `bin/`) is
released under the GNU General Public License, version 2 or later, as
Blender's add-on policy asks. The `frost` executable is proprietary
software, copyright the author, provided for use with this add-on; it is
not covered by the GPL and may not be redistributed separately.
