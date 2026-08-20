import { MotionConfig } from 'motion/react'
import { TaskExperience } from './TaskExperience'

/**
 * The TaskVM React island root. Wave A5 renders the mock-driven
 * experience; the shell host (`static/index.html`) mounts this build
 * into `<div id="task-experience-root">` in the next wave.
 *
 * `MotionConfig reducedMotion="user"` is the island's GLOBAL
 * prefers-reduced-motion opt-in (A7): framer-motion's default context
 * is `reducedMotion: "never"` — the library does NOT auto-respect the
 * OS preference; the app must opt in. "user" = honor the OS setting
 * everywhere (verified against framer-motion's MotionConfigContext
 * default + the A7 e2e's reduced-motion browser context).
 */
function App() {
  return (
    <MotionConfig reducedMotion="user">
      <TaskExperience />
    </MotionConfig>
  )
}

export default App
