import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { gsap } from 'gsap'
import './HeroScene3D.css'

const EDGE_LIMIT = 2.75

function randomPoint(radius = 4.2) {
  const angle = Math.random() * Math.PI * 2
  const ring = radius * (0.3 + Math.random() * 0.7)
  const z = (Math.random() - 0.5) * 2.6
  return new THREE.Vector3(Math.cos(angle) * ring, Math.sin(angle) * ring * 0.7, z)
}

function createNodes(count = 15) {
  return Array.from({ length: count }, () => randomPoint())
}

function buildConnections(nodes) {
  const edges = []
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      if (nodes[i].distanceTo(nodes[j]) < EDGE_LIMIT) {
        edges.push([i, j])
      }
    }
  }
  return edges
}

function createStars(count = 380) {
  const positions = new Float32Array(count * 3)
  for (let i = 0; i < count; i += 1) {
    positions[i * 3] = (Math.random() - 0.5) * 14
    positions[i * 3 + 1] = (Math.random() - 0.5) * 9
    positions[i * 3 + 2] = (Math.random() - 0.5) * 10
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    const material = new THREE.PointsMaterial({
      size: 0.03,
      color: 0xf5cdb3,
      transparent: true,
      opacity: 0.8,
      depthWrite: false,
  })
  return new THREE.Points(geometry, material)
}

export default function HeroScene3D() {
  const mountRef = useRef(null)

  useEffect(() => {
    const container = mountRef.current
    if (!container) return undefined

    const width = container.clientWidth || window.innerWidth
    const height = container.clientHeight || window.innerHeight
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

    let renderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' })
    } catch {
      return undefined
    }

    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8))
    renderer.setSize(width, height)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    container.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(44, width / height, 0.1, 100)
    camera.position.set(0, 0, 10.2)

    const world = new THREE.Group()
    scene.add(world)

    const stars = createStars()
    scene.add(stars)

    const nodes = createNodes()
    const edges = buildConnections(nodes)

    const nodeGroup = new THREE.Group()
    world.add(nodeGroup)

    const nodeMeshes = nodes.map((position, index) => {
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(0.16 + (index % 3) * 0.03, 20, 20),
        new THREE.MeshStandardMaterial({
          color: index % 2 ? 0x9cc3d7 : 0xff9a74,
          emissive: index % 2 ? 0x2b3f4b : 0x6b3528,
          emissiveIntensity: 0.8,
          roughness: 0.26,
          metalness: 0.2,
          transparent: true,
          opacity: 0.94,
        })
      )
      mesh.position.copy(position)
      nodeGroup.add(mesh)
      return mesh
    })

    const lineMaterial = new THREE.LineBasicMaterial({
      color: 0xe0b9a2,
      transparent: true,
      opacity: 0.25,
    })
    const lines = edges.map(([a, b]) => {
      const geometry = new THREE.BufferGeometry().setFromPoints([nodes[a], nodes[b]])
      const line = new THREE.Line(geometry, lineMaterial)
      nodeGroup.add(line)
      return line
    })

    const pulseEdges = edges.length > 0 ? edges : [[0, Math.min(1, nodes.length - 1)]]
    const pulses = Array.from({ length: Math.min(10, pulseEdges.length) }, (_, idx) => {
      const edgeIndex = idx % pulseEdges.length
      const pulse = new THREE.Mesh(
        new THREE.SphereGeometry(0.07, 14, 14),
        new THREE.MeshBasicMaterial({ color: 0xfff5ad, transparent: true, opacity: 0.95 })
      )
      pulse.userData = {
        edgeIndex,
        progress: Math.random(),
        speed: 0.15 + Math.random() * 0.32,
      }
      nodeGroup.add(pulse)
      return pulse
    })

    const ambient = new THREE.HemisphereLight(0xffd8bf, 0x1f151d, 1.05)
    scene.add(ambient)
    const key = new THREE.PointLight(0xff9c76, 16, 20, 2)
    key.position.set(3.6, 2.8, 5)
    scene.add(key)
    const warm = new THREE.PointLight(0x8dbad0, 10, 16, 2)
    warm.position.set(-4.5, -3.8, 4)
    scene.add(warm)

    const pointer = { x: 0, y: 0 }
    const targetRotation = { x: -0.1, y: 0.06 }
    const onPointer = (event) => {
      const rect = container.getBoundingClientRect()
      pointer.x = ((event.clientX - rect.left) / rect.width - 0.5) * 2
      pointer.y = ((event.clientY - rect.top) / rect.height - 0.5) * 2
    }
    container.addEventListener('pointermove', onPointer)

    const tl = gsap.timeline()
    tl.fromTo(
      world.scale,
      { x: 0.76, y: 0.76, z: 0.76 },
      { x: 1, y: 1, z: 1, duration: 1.1, ease: 'expo.out' }
    ).fromTo(
      camera.position,
      { z: 12.6 },
      { z: 10.2, duration: 1.2, ease: 'power3.out' },
      0
    )

    gsap.to(nodeGroup.rotation, {
      z: 0.18,
      duration: 10.5,
      repeat: -1,
      yoyo: true,
      ease: 'sine.inOut',
    })

    const clock = new THREE.Clock()
    let frameId
    const animate = () => {
      const dt = Math.min(clock.getDelta(), 0.035)
      const t = clock.elapsedTime

      targetRotation.x = THREE.MathUtils.lerp(targetRotation.x, -pointer.y * 0.22, 0.045)
      targetRotation.y = THREE.MathUtils.lerp(targetRotation.y, pointer.x * 0.28, 0.045)
      world.rotation.x = targetRotation.x
      world.rotation.y = targetRotation.y + Math.sin(t * 0.26) * 0.04

      stars.rotation.y += dt * 0.02
      stars.rotation.x = Math.sin(t * 0.12) * 0.06

      pulses.forEach((pulse) => {
        const data = pulse.userData
        data.progress += dt * (reduced ? data.speed * 0.24 : data.speed)
        if (data.progress >= 1) {
          data.progress = 0
          data.edgeIndex = (data.edgeIndex + 1 + Math.floor(Math.random() * 3)) % pulseEdges.length
        }
        const [aIdx, bIdx] = pulseEdges[data.edgeIndex]
        pulse.position.lerpVectors(nodes[aIdx], nodes[bIdx], data.progress)
      })

      nodeMeshes.forEach((mesh, idx) => {
        mesh.scale.setScalar(1 + Math.sin(t * 1.3 + idx) * 0.06)
      })

      renderer.render(scene, camera)
      frameId = window.requestAnimationFrame(animate)
    }
    animate()

    const onResize = () => {
      const w = container.clientWidth || window.innerWidth
      const h = container.clientHeight || window.innerHeight
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
    }
    window.addEventListener('resize', onResize)

    return () => {
      window.removeEventListener('resize', onResize)
      container.removeEventListener('pointermove', onPointer)
      window.cancelAnimationFrame(frameId)
      tl.kill()
      gsap.killTweensOf(nodeGroup.rotation)

      nodeMeshes.forEach((mesh) => {
        mesh.geometry.dispose()
        mesh.material.dispose()
      })
      lines.forEach((line) => line.geometry.dispose())
      lineMaterial.dispose()
      pulses.forEach((pulse) => {
        pulse.geometry.dispose()
        pulse.material.dispose()
      })
      stars.geometry.dispose()
      stars.material.dispose()
      renderer.dispose()

      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement)
      }
    }
  }, [])

  return <div className="hero-scene" ref={mountRef} aria-hidden="true" />
}
