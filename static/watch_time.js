/**
 * 将播放时长秒数格式化为可读中文时长字符串。
 * @param {number|null|undefined} seconds 播放时长秒数。
 * @returns {string} 格式化后的时长文本（如「1 小时 30 分钟」「45 秒」）。
 */
function formatWatchDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) {
        return minutes > 0 ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`;
    }
    if (minutes > 0) {
        return `${minutes} 分钟`;
    }
    return `${secs} 秒`;
}

/**
 * 格式化媒体账号的时长保活门槛进度文案（直接复用后端单源评估文本）。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @returns {string} 保活进度、达标状态或未开放提示文本。
 */
function formatWatchTimeRenewalProgress(acc) {
    if (!acc || !acc.watch_time_renewal_open) {
        return acc && acc.watch_time_renewal_status_text
            ? acc.watch_time_renewal_status_text
            : '当前服务器未开放保活续期';
    }
    return acc.watch_time_renewal_status_text || formatWatchDuration(acc.cycle_watch_time_seconds);
}

/**
 * 统一处理 Mini App 账号自动保活开关切换、错误回滚与结果提示。
 * @param {number|string} serverId 服务器 ID。
 * @param {object} tg Telegram WebApp 对象。
 * @param {HTMLInputElement|null} checkbox 触发切换的复选框元素。
 * @param {Function|null} onSuccess 可选成功回调，接收最终开关布尔状态。
 */
async function handleAutoRenewToggle(serverId, tg, checkbox, onSuccess = null) {
    const targetState = Boolean(checkbox && checkbox.checked);
    try {
        const res = await fetch(`/api/miniapp/accounts/${serverId}/toggle_auto_renew`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Telegram-Init-Data': tg.initData
            },
            body: JSON.stringify({ enabled: targetState })
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            throw new Error(data.detail || data.message || '切换自动保活失败');
        }
        const finalState = typeof data.new_state === 'boolean' ? data.new_state : targetState;
        if (onSuccess) onSuccess(finalState, data);
        tg.showAlert(data.message || (finalState ? '已开启自动保活' : '已关闭自动保活'));
    } catch (e) {
        if (checkbox) checkbox.checked = !targetState;
        tg.showAlert(e.message || '切换自动保活失败');
    }
}
