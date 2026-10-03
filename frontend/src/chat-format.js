import { marked } from 'marked';
import DOMPurify from 'dompurify';

// Model output is untrusted. Allow text formatting, never embedded media or controls.
export function formatAssistantMessage(text) {
  return DOMPurify.sanitize(marked.parse(text, {gfm: true, breaks: true}), {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'ul', 'ol', 'li', 'blockquote',
      'pre', 'code', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6'],
    ALLOWED_ATTR: ['href', 'title', 'start'],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
}
