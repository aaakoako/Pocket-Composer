/** 输入法可能只修改 textarea.value，在结束合成或失焦时才发 input。
 * 事件立即同步；前台轻量检查补上缺失的事件，不依赖点击插入。
 * rendered 绑定上次显示的状态，程序恢复/清空的新稿优先于旧 DOM，避免旧稿复活。
 */
export function installTextInputSync(input: HTMLTextAreaElement, options: {
  getText: () => string;
  canEdit: () => boolean;
  onEdit: (text: string) => void;
}) {
  let rendered = input.value;
  function reconcile(): boolean {
    if (!options.canEdit()) return false;
    const text = options.getText();
    if (text !== rendered || input.value === text) return false;
    options.onEdit(input.value);
    return true;
  }
  for (const event of ['input', 'compositionend', 'change', 'blur']) {
    input.addEventListener(event, reconcile);
  }
  window.setInterval(() => { if (!document.hidden) reconcile(); }, 200);
  return {
    reconcile,
    render() {
      reconcile();
      const text = options.getText();
      if (input.value !== text) input.value = text;
      rendered = text;
    },
  };
}
