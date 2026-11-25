"""Interactive studio-lit sphere and plane, rendered directly into the gallery."""
import gpu
from gpu_extras.batch import batch_for_shader


CHANNELS = ("Diffuse", "Normal", "Roughness", "Metalic", "Alpha", "aoMap", "Emission")
SAMPLERS = ("baseMap", "normalMap", "roughMap", "metalMap", "alphaMap", "occlusionMap", "emissionMap")

FRAGMENT = """
vec3 rotatePreview(vec3 v, vec2 a) {
    vec2 c = cos(a), s = sin(a);
    vec3 p = vec3(v.x, c.y*v.y - s.y*v.z, s.y*v.y + c.y*v.z);
    return vec3(c.x*p.x + s.x*p.z, p.y, c.x*p.z - s.x*p.x);
}

float softbox(vec3 reflected, vec3 direction, vec3 up, vec2 size, float rough) {
    float front = max(0.001, dot(reflected, direction));
    vec3 right = normalize(cross(up, direction));
    vec2 offset = abs(vec2(dot(reflected, right), dot(reflected, cross(direction, right)))) / front;
    float spread = 0.04 + 0.88 * rough * rough;
    vec2 shape = 1.0 - smoothstep(max(vec2(0.0), size - spread * 0.5), size + spread, offset);
    return shape.x * shape.y * smoothstep(0.0, 0.3, front) / (1.0 + 4.0 * rough * rough);
}

vec3 toLinear(vec3 srgb) {
    return mix(srgb / 12.92, pow((srgb + 0.055) / 1.055, vec3(2.4)),
               greaterThan(srgb, vec3(0.04045)));
}

void main() {
    vec2 angles = controls.xy;
    float magnification = controls.z;
    float planeView = controls.w;
    vec2 planeFit = material.xy;
    int flags = int(material.z);
    vec3 eye = vec3(0.0, 0.0, 3.2);
    vec3 ray = normalize(vec3(viewUV * (0.42 / magnification), -1.0));
    vec3 p;
    vec3 tangent;
    vec3 n;
    vec2 uv;
    if (planeView > 0.5) {
        vec3 o = rotatePreview(eye, angles);
        vec3 d = rotatePreview(ray, angles);
        if (abs(d.z) < 0.001 || -o.z/d.z <= 0.0) discard;
        p = o - d * o.z/d.z;
        uv = vec2(p.x, p.y) * planeFit * 0.5 + 0.5;
        if (any(lessThan(uv, vec2(0.0))) || any(greaterThan(uv, vec2(1.0)))) discard;
        n = vec3(0.0, 0.0, sign(o.z));
        tangent = vec3(1.0, 0.0, 0.0);
    } else {
        float b = dot(eye, ray);
        float disc = b*b - dot(eye, eye) + 1.0;
        if (disc < 0.0) discard;
        p = rotatePreview(eye + ray * (-b - sqrt(disc)), angles);
        n = normalize(p);
        float longitude = atan(n.z, n.x);
        uv = fract(vec2(longitude / 6.2831853 + 0.5, asin(n.y) / 3.14159265 + 0.5) * 2.0);
        tangent = vec3(-sin(longitude), 0.0, cos(longitude));
    }
    vec4 base = (flags & 1) != 0 ? texture(baseMap, uv) : vec4(0.56, 0.57, 0.59, 1.0);
    float alpha = (flags & 128) != 0 ? ((flags & 16) != 0 ? texture(alphaMap, uv).r : base.a) : 1.0;
    if (alpha < 0.003) discard;
    vec3 mapped = (flags & 2) != 0 ? texture(normalMap, uv).rgb * 2.0 - 1.0 : vec3(0.0, 0.0, 1.0);
    vec3 bitangent = cross(n, tangent) * (planeView > 0.5 ? 1.0 : -1.0);
    vec3 N = normalize(tangent*mapped.x + bitangent*mapped.y + n*max(0.06, mapped.z));
    float rough = (flags & 4) != 0 ? clamp(texture(roughMap, uv).r, 0.03, 1.0) : 0.46;
    float metal = (flags & 8) != 0 ? texture(metalMap, uv).r : 0.0;
    float ao = (flags & 32) != 0 ? texture(occlusionMap, uv).r : 1.0;
    vec3 emission = (flags & 64) != 0 ? toLinear(texture(emissionMap, uv).rgb) : vec3(0.0);
    vec3 V = normalize(rotatePreview(-ray, angles));
    vec3 up = rotatePreview(vec3(0.0, 1.0, 0.0), angles);
    vec3 key = rotatePreview(normalize(vec3(-0.56, 0.65, 0.51)), angles);
    vec3 fill = rotatePreview(normalize(vec3(0.76, 0.09, 0.64)), angles);
    vec3 rim = rotatePreview(normalize(vec3(0.81, 0.38, -0.44)), angles);
    vec3 albedo = toLinear(base.rgb);
    float shapeLight = max(0.0, dot(n, key));
    float detailLight = max(0.0, dot(N, key));
    float diffuse = 0.14 * ao + 1.10 * mix(shapeLight, detailLight, 0.72)
                    + 0.15 * max(0.0, dot(N, fill)) + 0.24 * max(0.0, dot(N, rim));

    vec3 reflected = reflect(-V, N);
    float sheen = 2.2 * softbox(reflected, key, up, vec2(0.45, 0.21), rough)
                  + 1.25 * softbox(reflected, rim, up, vec2(0.14, 0.60), rough);
    float facing = max(0.0, dot(N, V));
    float grazing = pow(1.0 - facing, 5.0) * (1.0 - 0.65 * rough);
    vec3 f0 = mix(vec3(0.04), albedo, metal);
    vec3 reflection = mix(f0, vec3(1.0), 0.24 * grazing) * sheen;
    // Bound the white component on dielectrics while keeping colored metal reflections.
    float luminance = dot(albedo, vec3(0.2126, 0.7152, 0.0722));
    float peak = mix(0.028 + 0.33 * luminance, 0.8, metal);
    reflection *= min(1.0, peak / max(0.001, max(reflection.r, max(reflection.g, reflection.b))));
    vec3 light = albedo * (diffuse * (1.0 - metal) + 0.19 * metal) + reflection + emission;
    // Luminance-based compression holds the base hue through the shoulder.
    float intensity = dot(light, vec3(0.2126, 0.7152, 0.0722));
    float mapping = (1.0 - exp(-intensity)) / max(intensity, 0.0001);
    fragColor = vec4(light * mapping, alpha);
}
"""


class LivePreview:
    def __init__(self):
        self.shader = None
        self.batch = None
        self.fallback = None

    def draw(self, center, radius, textures, angles, zoom, plane, alpha):
        if self.shader is None:
            stage = gpu.types.GPUStageInterfaceInfo("rtc_gallery_preview")
            stage.smooth("VEC2", "viewUV")
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant("MAT4", "mvp")
            for name in ("bounds", "controls", "material"):
                info.push_constant("VEC4", name)
            for index, name in enumerate(SAMPLERS):
                info.sampler(index, "FLOAT_2D", name)
            info.vertex_in(0, "VEC2", "pos")
            info.vertex_out(stage)
            info.fragment_out(0, "VEC4", "fragColor")
            info.vertex_source("""
                void main() {
                    viewUV = pos;
                    gl_Position = mvp * vec4(bounds.xy + pos*bounds.z, 0.0, 1.0);
                }
            """)
            info.fragment_source(FRAGMENT)
            self.shader = gpu.shader.create_from_info(info)
            self.batch = batch_for_shader(self.shader, "TRI_FAN",
                                          {"pos": ((-1, -1), (1, -1), (1, 1), (-1, 1))})
            self.fallback = gpu.types.GPUTexture((1, 1))
        shader = self.shader
        shader.bind()
        shader.uniform_float("mvp", gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
        shader.uniform_float("bounds", (*center, radius, 0.0))
        shader.uniform_float("controls", (*angles, zoom, float(plane)))
        width, height = textures["Diffuse"][1] if "Diffuse" in textures else (1, 1)
        flags = sum(1 << index for index, channel in enumerate(CHANNELS) if channel in textures)
        if alpha:
            flags |= 1 << len(CHANNELS)
        shader.uniform_float("material", (max(1, height / width), max(1, width / height), float(flags), 0.0))
        for channel, sampler in zip(CHANNELS, SAMPLERS):
            shader.uniform_sampler(sampler, textures[channel][0] if channel in textures else self.fallback)
        gpu.state.blend_set("ALPHA")
        try:
            self.batch.draw(shader)
        finally:
            gpu.state.blend_set("NONE")
