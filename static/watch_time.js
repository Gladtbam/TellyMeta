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
