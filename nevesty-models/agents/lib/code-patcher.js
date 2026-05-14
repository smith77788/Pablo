/**
 * Code Patcher — safe file modification utility for fix agents
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '../..');

function readFile(relPath) {
  return fs.readFileSync(path.join(ROOT, relPath), 'utf8');
}

function writeFile(relPath, content) {
  const fullPath = path.join(ROOT, relPath);
  // Backup before writing
  const backup = fullPath + '.bak';
  if (fs.existsSync(fullPath)) {
    fs.writeFileSync(backup, fs.readFileSync(fullPath));
  }
  fs.writeFileSync(fullPath, content);
  // Remove backup if write succeeded
  if (fs.existsSync(backup)) fs.unlinkSync(backup);
  return true;
}

function patchString(relPath, oldStr, newStr) {
  const content = readFile(relPath);
  if (!content.includes(oldStr)) {
    throw new Error(`Pattern not found in ${relPath}: ${oldStr.slice(0, 80)}`);
  }
  const patched = content.replace(oldStr, newStr);
  writeFile(relPath, patched);
  return true;
}

function patchRegex(relPath, regex, replacement) {
  const content = readFile(relPath);
  const patched = content.replace(regex, replacement);
  if (patched === content) throw new Error(`Regex had no effect in ${relPath}`);
  writeFile(relPath, patched);
  return true;
}

function appendToFile(relPath, code) {
  const content = readFile(relPath);
  // Insert before last process.on or module.exports
  const insertBefore = 'process.on(\'unhandledRejection\'';
  if (content.includes(insertBefore)) {
    writeFile(relPath, content.replace(insertBefore, code + '\n\n' + insertBefore));
  } else {
    writeFile(relPath, content + '\n\n' + code);
  }
  return true;
}

function countOccurrences(relPath, pattern) {
  const content = readFile(relPath);
  return (content.match(new RegExp(pattern, 'g')) || []).length;
}

module.exports = { readFile, writeFile, patchString, patchRegex, appendToFile, countOccurrences };
