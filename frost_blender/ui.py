# The Frost rows in Render Properties, and Blender's own material, light,
# camera and world panels kept on when Frost is the engine.

import bpy

from . import properties


class RENDER_PT_frost(bpy.types.Panel):
    bl_label = "Frost"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"
    COMPAT_ENGINES = {'FROST'}

    @classmethod
    def poll(cls, context):
        return context.engine == 'FROST'

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        settings = context.scene.frost
        column = layout.column(align=True)
        column.prop(settings, "samples")
        column.prop(settings, "bounces")
        layout.prop(settings, "denoise")
        layout.prop(settings, "adaptive")
        row = layout.row()
        row.active = settings.adaptive
        row.prop(settings, "noise_threshold")
        layout.prop(settings, "filter_glossy")
        layout.prop(settings, "exposure")

        frost = properties.find_frost(properties.preferences(context))
        box = layout.box()
        if frost:
            box.label(text="Frost: " + frost, icon='CHECKMARK')
        else:
            box.label(text="Frost was not found; see the add-on's preferences.", icon='ERROR')
        if context.scene.view_settings.view_transform != 'Standard':
            box.label(text="Frost's frame is already tonemapped: use the Standard view transform.", icon='INFO')
            box.operator("frost.standard_view", text="Use Standard")


class FROST_OT_standard_view(bpy.types.Operator):
    bl_idname = "frost.standard_view"
    bl_label = "Use the Standard view transform"
    bl_description = "Frost tonemaps its own frame; the Standard view shows it as rendered"

    def execute(self, context):
        context.scene.view_settings.view_transform = 'Standard'
        context.scene.view_settings.look = 'None'
        return {'FINISHED'}


def blender_panels():
    """Blender's own property panels that show for the built-in engines, so
    materials, lights, cameras and the world keep their panels under Frost."""
    exclude = {
        'RENDER_PT_eevee_ambient_occlusion', 'RENDER_PT_eevee_motion_blur', 'RENDER_PT_eevee_next_motion_blur',
        'RENDER_PT_eevee_depth_of_field', 'RENDER_PT_eevee_bloom', 'RENDER_PT_eevee_volumetric',
        'RENDER_PT_eevee_subsurface_scattering', 'RENDER_PT_eevee_screen_space_reflections',
        'RENDER_PT_eevee_shadows', 'RENDER_PT_eevee_indirect_lighting', 'RENDER_PT_eevee_film',
        'RENDER_PT_eevee_hair', 'RENDER_PT_eevee_performance', 'RENDER_PT_eevee_sampling',
        'RENDER_PT_eevee_next_sampling', 'RENDER_PT_eevee_next_raytracing', 'RENDER_PT_eevee_next_volumes',
        'RENDER_PT_eevee_next_film', 'RENDER_PT_eevee_next_clamping', 'RENDER_PT_eevee_next_shadows',
        'RENDER_PT_eevee_next_lights', 'RENDER_PT_eevee_next_performance',
        'RENDER_PT_simplify', 'RENDER_PT_freestyle', 'RENDER_PT_motion_blur', 'RENDER_PT_opengl_sampling',
        'RENDER_PT_opengl_lighting', 'RENDER_PT_opengl_color', 'RENDER_PT_opengl_options',
        'RENDER_PT_opengl_film', 'RENDER_PT_color_management_curves',
    }
    panels = []
    for panel in bpy.types.Panel.__subclasses__():
        compat = getattr(panel, "COMPAT_ENGINES", None)
        if compat and 'BLENDER_RENDER' in compat and panel.__name__ not in exclude:
            panels.append(panel)
    return panels


classes = (RENDER_PT_frost, FROST_OT_standard_view)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    for panel in blender_panels():
        panel.COMPAT_ENGINES.add('FROST')


def unregister():
    for panel in blender_panels():
        panel.COMPAT_ENGINES.discard('FROST')
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
