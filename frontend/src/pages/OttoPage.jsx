import { Apple, Command, MousePointerClick, Brain, Hand, ShieldCheck } from 'lucide-react'
import Topbar from '../layout/Topbar'
import Button from '../ui/Button'
import { usePageMeta } from '../seo/usePageMeta'
import './OttoPage.css'

const VERSION = '0.3.0'
const DMG_HREF = `/otto/Otto-${VERSION}.dmg`

const FEATURES = [
  {
    icon: Command,
    title: 'Summon in a tap',
    body: 'Double-tap Right ⌘ for the typed bar, or hold Right ⌘ to talk. Otto appears over whatever you’re already doing.',
  },
  {
    icon: MousePointerClick,
    title: 'Acts in your real apps',
    body: 'Drives the foreground app through the macOS accessibility tree, the browser (Arc, Chrome, Safari, Brave), or — when an app exposes no structure — by seeing the screen.',
  },
  {
    icon: Brain,
    title: 'Knows your context',
    body: 'An always-on layer reads your screen on-device into a private memory, plus a profile you edit — so Otto acts with your context instead of stopping to ask.',
  },
  {
    icon: Hand,
    title: 'Stays in your control',
    body: 'Otto asks when it’s unsure instead of inventing details, and you can stop it at any moment by clicking the mouse or double-tapping Esc.',
  },
]

export default function OttoPage() {
  usePageMeta({
    title: 'Otto — a native macOS agent that does your computer work',
    description:
      'Otto is a native macOS agent. Summon it, say what you want in plain words, and it operates your real apps and websites — watched, live — using what it already knows about you.',
    ogImage: '/hero-adam-square.png',
  })

  const handleDownload = () => {
    const a = document.createElement('a')
    a.href = DMG_HREF
    a.download = `Otto-${VERSION}.dmg`
    document.body.appendChild(a)
    a.click()
    a.remove()
  }

  return (
    <main className="otto">
      <Topbar crumbs={[{ label: 'Otto' }]} />
      <div className="otto__scroll">
        <div className="otto__content">
          {/* Hero */}
          <section className="otto__hero">
            <p className="otto__eyebrow">Native macOS app</p>
            <h1 className="otto__h1">Otto does your computer work, as you.</h1>
            <p className="otto__lead">
              Summon Otto, say or type what you want in plain words, and it operates your real apps
              and websites — watched, live — using what it already knows about you.
            </p>
            <div className="otto__cta-row">
              <Button variant="primary" size="lg" icon={<Apple size={18} />} onClick={handleDownload}>
                Download for macOS
              </Button>
            </div>
            <p className="otto__meta">Version {VERSION} · macOS 14 Sonoma or later · Apple Silicon</p>
          </section>

          {/* Install */}
          <section className="otto__card otto__install">
            <h2 className="otto__h2">Installing Otto</h2>
            <p className="otto__muted">
              Otto is a young app and isn’t notarized by Apple yet, so macOS asks you to confirm the
              first launch. You only do this once.
            </p>
            <ol className="otto__steps">
              <li>
                Open the downloaded <strong>Otto.dmg</strong> and drag <strong>Otto</strong> onto the{' '}
                <strong>Applications</strong> folder.
              </li>
              <li>
                Open <strong>Otto</strong> from Applications. macOS shows{' '}
                <em>“Apple could not verify Otto is free of malware.”</em> — click <strong>Done</strong>.
              </li>
              <li>
                Open <strong>System&nbsp;Settings → Privacy&nbsp;&amp;&nbsp;Security</strong>, scroll to
                the message about Otto, and click <strong>Open&nbsp;Anyway</strong>. Confirm with
                Touch&nbsp;ID or your password.
              </li>
            </ol>
            <p className="otto__muted otto__alt">
              Comfortable in the terminal? Run{' '}
              <code>xattr -dr com.apple.quarantine /Applications/Otto.app</code>, then open Otto normally.
            </p>
          </section>

          {/* Features */}
          <section className="otto__features">
            <h2 className="otto__h2 otto__h2--center">What it does</h2>
            <div className="otto__grid">
              {FEATURES.map(({ icon: Icon, title, body }) => (
                <article key={title} className="otto__card otto__feature">
                  <span className="otto__feature-icon">
                    <Icon size={20} />
                  </span>
                  <h3 className="otto__feature-title">{title}</h3>
                  <p className="otto__muted">{body}</p>
                </article>
              ))}
            </div>
          </section>

          {/* Privacy */}
          <section className="otto__card otto__privacy">
            <span className="otto__privacy-icon">
              <ShieldCheck size={22} />
            </span>
            <div>
              <h2 className="otto__h2">Private by design</h2>
              <p className="otto__muted">
                Screenshots are read as text on your Mac with Vision OCR — the image never leaves the
                device. Your memory and profile live in <code>~/.otto</code>, readable only by you. When
                you give a command, Otto sends just that task’s context to the model.
              </p>
            </div>
          </section>

          {/* Footer CTA */}
          <footer className="otto__footer">
            <Button variant="primary" size="lg" icon={<Apple size={18} />} onClick={handleDownload}>
              Download Otto for macOS
            </Button>
            <p className="otto__meta">Version {VERSION} · macOS 14 Sonoma or later · Apple Silicon</p>
          </footer>
        </div>
      </div>
    </main>
  )
}
