import { TaskExperience } from './TaskExperience'

/**
 * The TaskVM React island root. Wave A5 renders the mock-driven
 * experience; the shell host (`static/index.html`) mounts this build
 * into `<div id="task-experience-root">` in the next wave.
 */
function App() {
  return <TaskExperience />
}

export default App
