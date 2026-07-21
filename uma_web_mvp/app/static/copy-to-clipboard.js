// copy-to-clipboard.js — 复制图片到剪贴板（PC端专用）
// 仅支持 (hover: hover) and (pointer: fine) 的桌面环境

let _copyImageLock = false;

/**
 * 判断是否为支持精确指针的桌面环境
 * 用于 JS 端控制是否显示复制按钮
 */
function isDesktopWithPointer() {
  return window.matchMedia('(hover: hover) and (pointer: fine)').matches;
}

/**
 * 将图片 URL 复制到剪贴板（真实图片数据，非 URL 文本）
 * @param {string} imageUrl - 图片的真实 URL（同源，携带 Cookie）
 * @returns {Promise<boolean>} 成功返回 true
 */
async function copyImageToClipboard(imageUrl) {
  if (_copyImageLock) {
    return false;
  }

  if (!navigator.clipboard || !window.ClipboardItem) {
    throw new Error('当前浏览器不支持直接复制图片，请使用保存图片功能。');
  }

  _copyImageLock = true;
  try {
    const response = await fetch(imageUrl, {
      credentials: 'same-origin',
      cache: 'no-store',
    });

    if (!response.ok) {
      throw new Error(response.status === 401 ? '图片鉴权失败，请刷新页面后重试。' : '图片加载失败');
    }

    const blob = await response.blob();
    let pngBlob;

    if (blob.type === 'image/png') {
      pngBlob = blob;
    } else {
      const bitmap = await createImageBitmap(blob);
      try {
        const canvas = document.createElement('canvas');
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(bitmap, 0, 0);

        pngBlob = await new Promise((resolve, reject) => {
          canvas.toBlob((b) => {
            if (b) resolve(b);
            else reject(new Error('图片转换失败'));
          }, 'image/png');
        });
      } finally {
        bitmap.close();
      }
    }

    await navigator.clipboard.write([
      new ClipboardItem({
        'image/png': pngBlob,
      }),
    ]);

    return true;
  } finally {
    _copyImageLock = false;
  }
}

/**
 * PC端简易图片下载（用于 smart-agent 等没有 app.js 依赖的页面）
 * @param {string} url - 图片 URL
 * @param {string} filename - 文件名
 */
async function downloadImageSimple(url, filename) {
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error('下载失败');
  const blob = await res.blob();
  const blobUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = filename || 'image.png';
    document.body.append(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(blobUrl);
  }
}
