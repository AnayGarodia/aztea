import { useEffect, useRef } from 'react'
import './AnimatedShaderHero.css'

const VERTEX_SHADER = `#version 300 es
precision highp float;
in vec2 position;
void main() {
  gl_Position = vec4(position, 0.0, 1.0);
}`

const FRAGMENT_SHADER = `#version 300 es
precision highp float;
out vec4 O;
uniform vec2 resolution;
uniform float time;
uniform vec3 tint;
uniform vec3 paletteBase;
uniform float brightness;
uniform float xShift;

#define FC gl_FragCoord.xy
#define T time
#define R resolution
#define MN min(R.x, R.y)

float rnd(vec2 p) {
  p = fract(p * vec2(12.9898, 78.233));
  p += dot(p, p + 34.56);
  return fract(p.x * p.y);
}

float noise(in vec2 p) {
  vec2 i = floor(p), f = fract(p), u = f * f * (3.0 - 2.0 * f);
  float a = rnd(i);
  float b = rnd(i + vec2(1.0, 0.0));
  float c = rnd(i + vec2(0.0, 1.0));
  float d = rnd(i + vec2(1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float t = 0.0;
  float a = 1.0;
  mat2 m = mat2(1.0, -0.5, 0.2, 1.2);
  for (int i = 0; i < 5; i++) {
    t += a * noise(p);
    p *= 2.0 * m;
    a *= 0.5;
  }
  return t;
}

float clouds(vec2 p) {
  float d = 1.0;
  float t = 0.0;
  for (float i = 0.0; i < 3.0; i++) {
    float a = d * fbm(i * 10.0 + p.x * 0.2 + 0.2 * (1.0 + i) * p.y + d + i * i + p);
    t = mix(t, d, a);
    d = a;
    p *= 2.0 / (i + 1.0);
  }
  return t;
}

void main() {
  vec2 uv = (FC - 0.5 * R) / MN;
  vec2 st = uv * vec2(2.0, 1.0);
  vec3 col = vec3(0.0);
  float bg = clouds(vec2(st.x + T * 0.5, -st.y));
  uv.x += xShift;

  uv *= 1.0 - 0.3 * (sin(T * 0.2) * 0.5 + 0.5);
  for (float i = 1.0; i < 12.0; i++) {
    uv += 0.1 * cos(i * vec2(0.1 + 0.01 * i, 0.8) + i * i + T * 0.5 + 0.1 * uv.x);
    vec2 p = uv;
    float d = max(length(p), 0.04);
    col += 0.00125 / d * (cos(sin(i) * vec3(1.0, 2.0, 3.0)) + 1.0);
    float b = noise(i + p + bg * 1.731);
    col += 0.002 * b / length(max(p, vec2(b * p.x * 0.02, p.y)));
    col = mix(col, paletteBase * bg, d);
  }

  col = mix(col, col * tint, 0.35);
  col *= brightness;
  O = vec4(col, 1.0);
}`

function createShader(gl, type, source) {
  const shader = gl.createShader(type)
  if (!shader) return null
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader)
    return null
  }
  return shader
}

function createProgram(gl, vertexSource, fragmentSource) {
  const vertex = createShader(gl, gl.VERTEX_SHADER, vertexSource)
  const fragment = createShader(gl, gl.FRAGMENT_SHADER, fragmentSource)
  if (!vertex || !fragment) return null
  const program = gl.createProgram()
  if (!program) return null
  gl.attachShader(program, vertex)
  gl.attachShader(program, fragment)
  gl.linkProgram(program)
  gl.deleteShader(vertex)
  gl.deleteShader(fragment)
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    gl.deleteProgram(program)
    return null
  }
  return program
}

export default function AnimatedShaderHero({ isDark = false, className = '' }) {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined

    const gl = canvas.getContext('webgl2', { alpha: false, antialias: false })
    if (!gl) return undefined

    const program = createProgram(gl, VERTEX_SHADER, FRAGMENT_SHADER)
    if (!program) return undefined

    const vertices = new Float32Array([
      -1, -1,
      1, -1,
      -1, 1,
      1, 1,
    ])
    const buffer = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW)

    const positionLoc = gl.getAttribLocation(program, 'position')
    const resolutionLoc = gl.getUniformLocation(program, 'resolution')
    const timeLoc = gl.getUniformLocation(program, 'time')
    const tintLoc = gl.getUniformLocation(program, 'tint')
    const paletteBaseLoc = gl.getUniformLocation(program, 'paletteBase')
    const brightnessLoc = gl.getUniformLocation(program, 'brightness')
    const xShiftLoc = gl.getUniformLocation(program, 'xShift')

    gl.enableVertexAttribArray(positionLoc)
    gl.vertexAttribPointer(positionLoc, 2, gl.FLOAT, false, 0, 0)

    let frameId = 0

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const dpr = Math.max(1, Math.min(2, window.devicePixelRatio * 0.75))
      const width = Math.max(1, Math.floor(rect.width * dpr))
      const height = Math.max(1, Math.floor(rect.height * dpr))
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width
        canvas.height = height
      }
      gl.viewport(0, 0, width, height)
    }

    const render = (now) => {
      resize()
      gl.useProgram(program)
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
      gl.uniform2f(resolutionLoc, canvas.width, canvas.height)
      gl.uniform1f(timeLoc, now * 0.001)
      if (isDark) {
        gl.uniform3f(tintLoc, 0.44, 0.66, 0.95)
        gl.uniform3f(paletteBaseLoc, 0.08, 0.14, 0.34)
        gl.uniform1f(brightnessLoc, 0.8)
        gl.uniform1f(xShiftLoc, -0.52)
      } else {
        gl.uniform3f(tintLoc, 0.94, 0.82, 0.62)
        gl.uniform3f(paletteBaseLoc, 0.34, 0.24, 0.14)
        gl.uniform1f(brightnessLoc, 1.08)
        gl.uniform1f(xShiftLoc, -0.14)
      }
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
      frameId = requestAnimationFrame(render)
    }

    frameId = requestAnimationFrame(render)
    window.addEventListener('resize', resize)

    return () => {
      window.removeEventListener('resize', resize)
      cancelAnimationFrame(frameId)
      if (buffer) gl.deleteBuffer(buffer)
      gl.deleteProgram(program)
    }
  }, [isDark])

  const cls = [
    'animated-shader-hero',
    isDark ? 'animated-shader-hero--dark' : 'animated-shader-hero--light',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={cls} aria-hidden>
      <canvas ref={canvasRef} className="animated-shader-hero__canvas" />
      <div className="animated-shader-hero__veil" />
    </div>
  )
}
