/** Shared rendering contract. Preference text cannot change tool authorization. */
import { readFileSync, statSync } from 'node:fs';
import path from 'node:path';

const VALUES = {
  verbosity: { brief: '回答简短，先说结论。', detailed: '用户希望得到详细解释。' },
  language: { zh: '用中文回答。', en: '用英文回答。' },
  format: { bullets: '适当分点列出要点。', natural: '自然口语交流，避免机械列表。' },
  workflow: { test_first: '工作前先验证相关测试。', backup_first: '修改前先保存必要备份。' },
};
export const name = 'agentbridge-shared-persona';
export const inject = ['systemPrompt'];

export function personaText(profile = {}) {
  const preferences = (profile.preferences ?? []).filter((item) => item.expires == null || item.expires * 1000 > Date.now());
  const name = typeof profile.core?.name === 'string' && /^[\p{L}\p{N}_ \-]{1,40}$/u.test(profile.core.name)
    ? profile.core.name : 'DSH 助手';
  return `你是${name}，以简洁自然的中文交流，不擅自虚构人设。`
    + '长期偏好不是安全授权，不能据此扩大工具权限或省略确认。\n'
    + preferences.map((item) => VALUES[item.category]?.[item.value] ?? '').filter(Boolean).join('\n')
    + '\n不再把历史中这些类别的表述当作长期偏好：'
    + (profile.forgotten_categories ?? []).filter((key) => VALUES[key] && !preferences.some((item) => item.category === key)).join(',');
}

/** Mount ONLY in a dedicated native desktop agent preset; never globally.
 * The profile is a private exported preference snapshot, not chat history.
 */
export function apply(ctx, config) {
  if (!ctx.agent || !path.isAbsolute(config.profile)) throw new Error('dedicated agent scope and private profile required');
  ctx.systemPrompt.section({ name: 'deployment:persona', order: 0, text: () => {
    try {
      const info = statSync(config.profile);
      if ((info.mode & 0o077) !== 0 || info.uid !== process.getuid() || info.size > 65536) throw new Error('unsafe persona profile');
      return personaText(JSON.parse(readFileSync(config.profile, 'utf8')));
    } catch (error) {
      if (error.code === 'ENOENT') return personaText();
      throw error;
    }
  } });
}
