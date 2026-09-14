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
  applied, instances included.
- Materials through the Principled BSDF: base colour, metallic, roughness
  (as values or image textures), normal maps, emission with its strength,
  and transmission with its IOR. Files imported from glTF come through
  exactly, factors and packed maps included.
- Lights: one sun, point lights, spot lights, and area lights (rendered as
  the emissive surfaces they are, at the power you set).
- The world's colour and strength.
- The camera: focal length, sensor, clipping, and depth of field when it is
  on.

Frost's own rows are in Render Properties: samples, bounces, denoise,
adaptive sampling with its noise threshold, filter glossy, and exposure.

## What it does not do yet

- The rendered viewport mode. The viewport draws with EEVEE or Solid; Frost
  renders the final frame.
- Animation and motion blur through the add-on. Frost itself does both;
  the add-on renders the current frame.
- Volumes, hair, the Sky Texture world node, HDRI worlds, and shader node
  trees beyond the Principled BSDF and image textures. A material Frost
  cannot read renders with the values on its Principled sockets.
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
