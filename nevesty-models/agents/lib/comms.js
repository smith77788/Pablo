/**
 * Agent Communication Layer — "neural network" between agents
 * Agents post findings, proposals, and fixes here.
 * Other agents read and respond to each other's messages.
 */
const { dbRun, dbAll, dbGet } = require('./base');

// Post a finding to the shared blackboard
async function postFinding({ agentName, severity, message, file, line, autoFixable, proposedFix }) {
  return dbRun(
    `INSERT INTO agent_findings (agent_name, severity, message, file, line, auto_fixable, proposed_fix, status)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'open')`,
    [agentName, severity, message, file || null, line || null, autoFixable ? 1 : 0, proposedFix || null]
  );
}

// Get all open findings that haven't been fixed yet
async function getOpenFindings(severity = null) {
  const where = severity ? `WHERE status='open' AND severity=?` : `WHERE status='open'`;
  const params = severity ? [severity] : [];
  return dbAll(
    `SELECT * FROM agent_findings ${where} ORDER BY
     CASE severity WHEN '🔴' THEN 1 WHEN '🟠' THEN 2 WHEN '🟡' THEN 3 ELSE 4 END, created_at DESC`,
    params
  );
}

// Claim a finding for fixing (mark as in_progress)
async function claimFinding(findingId, fixerName) {
  return dbRun(
    `UPDATE agent_findings SET status='in_progress', claimed_by=?, claimed_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'`,
    [fixerName, findingId]
  );
}

// Mark a finding as fixed
async function markFixed(findingId, fixerName, diffSummary) {
  return dbRun(
    `UPDATE agent_findings SET status='fixed', fixed_by=?, fix_summary=?, fixed_at=CURRENT_TIMESTAMP WHERE id=?`,
    [fixerName, diffSummary || '', findingId]
  );
}

// Mark as unable to fix (needs human)
async function markManual(findingId, reason) {
  return dbRun(
    `UPDATE agent_findings SET status='manual', fix_summary=? WHERE id=?`,
    [reason || 'Requires manual intervention', findingId]
  );
}

// Post a discussion message (agents talking to each other)
async function discuss({ from, to, topic, message, refFindingId }) {
  return dbRun(
    `INSERT INTO agent_discussions (from_agent, to_agent, topic, message, ref_finding_id)
     VALUES (?, ?, ?, ?, ?)`,
    [from, to || 'all', topic, message, refFindingId || null]
  );
}

// Get discussion thread for a finding
async function getDiscussion(findingId) {
  return dbAll(
    `SELECT * FROM agent_discussions WHERE ref_finding_id=? ORDER BY created_at ASC`,
    [findingId]
  );
}

// Get recent unread messages for an agent
async function getMessages(agentName, sinceMinutes = 60) {
  return dbAll(
    `SELECT * FROM agent_discussions
     WHERE (to_agent=? OR to_agent='all')
     AND created_at > datetime('now', '-' || ? || ' minutes')
     ORDER BY created_at DESC LIMIT 50`,
    [agentName, sinceMinutes]
  );
}

module.exports = { postFinding, getOpenFindings, claimFinding, markFixed, markManual, discuss, getDiscussion, getMessages };
