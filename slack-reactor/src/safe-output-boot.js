// Imported first by index.js: ES modules run in import order, so this installs
// the output protection before @slack/bolt or anything else can print.
import { installSafeOutput } from './safe-output.js'

installSafeOutput()
