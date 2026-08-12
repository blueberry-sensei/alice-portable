// Probe: Electron mang sẵn node nào, có node:sqlite không.
// Chạy: electron.exe với ELECTRON_RUN_AS_NODE=1 (M-0060 — Electron *là* node).
let sqliteState;
try {
  const { DatabaseSync } = require('node:sqlite');
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE t(a TEXT)');
  db.prepare('INSERT INTO t VALUES (?)').run('xin chào');
  sqliteState = db.prepare('SELECT a FROM t').get().a;
  db.close();
} catch (e) {
  sqliteState = 'ERR: ' + e.message;
}

let ftsState;
try {
  const { DatabaseSync } = require('node:sqlite');
  const db = new DatabaseSync(':memory:');
  db.exec("CREATE VIRTUAL TABLE ft USING fts5(body)");
  db.prepare('INSERT INTO ft VALUES (?)').run('alice brain portable');
  ftsState = db.prepare("SELECT count(*) c FROM ft WHERE ft MATCH 'portable'").get().c;
  db.close();
} catch (e) {
  ftsState = 'ERR: ' + e.message;
}

console.log(JSON.stringify({
  electron: process.versions.electron,
  node: process.versions.node,
  chrome: process.versions.chrome,
  sqlite: sqliteState,
  fts5: ftsState,
}, null, 2));
