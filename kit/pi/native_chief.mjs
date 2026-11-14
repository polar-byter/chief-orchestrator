/** Терминал Pi открывает сессию Чифа, привязанную к журналу задачи. */
import { readFile, unlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { callBroker, createNativeControlServer } from './native_chief_control.mjs';
import { createChiefExtension } from './native_chief_extension.mjs';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
await unlink(process.argv[2]);
const projectRoot = resolve(options.root);
const sdk = await import(pathToFileURL(join(options.packageRoot, 'dist/index.js')).href);
const state = { session: null, identifier: null, task: null, settled: true, removing: false, context: null };
const control = await createNativeControlServer(state);
process.on('exit', () => {
  if (state.task && state.identifier && !state.removing && process.stdout.isTTY && !process.stdout.destroyed) {
    const python = process.platform === 'win32' ? '.\\kit\\python.cmd' : 'sh kit/python.sh';
    try { process.stdout.write(`Продолжить задачу: ${python} -m kit.run_pi --session ${state.identifier}\n`); }
    catch { /* ОС могла уже закрыть терминал. */ }
  }
});
const sessionDirectory = join(projectRoot, '.pi/kit-service/sessions');
const manager = options.transcript ? sdk.SessionManager.open(options.transcript, sessionDirectory)
  : sdk.SessionManager.create(projectRoot, sessionDirectory);

async function createRuntime({ cwd, sessionManager, sessionStartEvent }) {
  if (resolve(cwd) !== projectRoot) throw new Error('Запустите kit.run_pi в каталоге другого проекта.');
  const configuration = JSON.parse(await readFile(join(projectRoot, '.pi/kit-runtime.json'), 'utf8'));
  const chief = configuration.roles.build;
  const services = await sdk.createAgentSessionServices({ cwd, agentDir: configuration.agent_directory,
    resourceLoaderOptions: { noExtensions: true, disabledBuiltinExtensions: ['mcp'],
      noSkills: true, noPromptTemplates: true, noThemes: false,
      additionalSkillPaths: [join(projectRoot, 'kit/skills/chief-orchestrator')],
      appendSystemPrompt: [chief.system_prompt],
      extensionFactories: [createChiefExtension(projectRoot, state, options)] } });
  const selectedModel = services.modelRuntime.getModel(chief.provider, chief.model);
  if (!selectedModel) throw new Error('Модель Чифа недоступна. Проверьте .pi/kit-models.json и повторите подготовку с --prepare-only.');
  const created = await sdk.createAgentSessionFromServices({ services, sessionManager, sessionStartEvent,
    model: selectedModel, tools: ['read', process.platform === 'win32' ? 'powershell' : 'bash', 'edit', 'write', 'kit_browser', 'kit_task_report'] });
  if (created.session.model?.provider !== chief.provider || created.session.model?.id !== chief.model) {
    throw new Error('Pi did not keep the configured Chief model');
  }
  created.session.setAutoRetryEnabled(false);
  state.session = created.session;
  state.identifier = options.session && !state.task ? options.session : 'ses_' + sessionManager.getSessionId().replaceAll('-', '');
  state.settled = true;
  state.removing = false;
  const attached = await callBroker(projectRoot, 'session.attachInteractive', {}, {
    id: state.identifier, transcript: created.session.sessionFile, pid: process.pid,
    port: control.port, token: control.token, task: state.task ? undefined : options.task });
  state.task = attached.data.task;
  return { ...created, services, diagnostics: services.diagnostics };
}

try {
  const runtime = await sdk.createAgentSessionRuntime(createRuntime, {
    cwd: projectRoot, agentDir: join(projectRoot, '.pi/agent'), sessionManager: manager });
  const interactive = new sdk.InteractiveMode(runtime, { initialMessage: options.prompt || undefined });
  await interactive.run();
} catch (error) {
  control.server.close();
  process.stderr.write('Pi Kit: ' + error.message + '\n');
  process.exitCode = 1;
}
