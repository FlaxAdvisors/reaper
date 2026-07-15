async function initializeLogger() {
  const chalk = await import('chalk');

  const levels = {
    info: chalk.default.blue, // Accessing default export
    warn: chalk.default.yellow,
    error: chalk.default.red,
    debug: chalk.default.green
  };

  return {
    info: (msg: string) => console.log(levels.info('[INFO]'), msg),
    warn: (msg: string) => console.warn(levels.warn('[WARN]'), msg),
    error: (msg: string) => console.error(levels.error('[ERROR]'), msg),
    debug: (msg: string) => console.debug(levels.debug('[DEBUG]'), msg),
  };
}

let loggerInstance: {
  info: (msg: string) => void;
  warn: (msg: string) => void;
  error: (msg: string) => void;
  debug: (msg: string) => void;
} | null = null;

initializeLogger().then(logger => {
  loggerInstance = logger;
});

export const logger = {
  info: (msg: string) => {
    if (loggerInstance) loggerInstance.info(msg);
    else console.log('[INFO]', msg); // Fallback if not initialized yet
  },
  warn: (msg: string) => {
    if (loggerInstance) loggerInstance.warn(msg);
    else console.warn('[WARN]', msg);
  },
  error: (msg: string) => {
    if (loggerInstance) loggerInstance.error(msg);
    else console.error('[ERROR]', msg);
  },
  debug: (msg: string) => {
    if (loggerInstance) loggerInstance.debug(msg);
    else console.debug('[DEBUG]', msg);
  },
};