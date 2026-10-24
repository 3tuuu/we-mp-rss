# 微信读书公众号分页与登录维护

在现有 WeRSS 中启用公众号多篇增量、历史分页和正文补抓。移动端列表授权与 Web 正文授权分别保存、分别续期。

## 启用与使用

1. 按原项目方式安装依赖、初始化数据库，构建前端。将采集模式设为 weread_mp，启用正文采集：
   - GATHER.MODEL=weread_mp
   - GATHER.CONTENT=True
2. 登录 WeRSS，进入导航“微信读书”（/weread）。
3. 点击“Web 正文扫码授权”完成正文授权；点击“移动端扫码授权”完成列表授权。
4. 在原订阅管理页新增公众号即可自动排队采集。后续定时更新使用同一采集器，无需逐号验证或启用。
5. 需要更早文章时，在“历史分页与诊断”选择已订阅公众号，点击回补。下一批可点击“从 next_offset 继续回补”。

默认每次最多 5 页、每页 20 条；整页都已入库、接口返回结束、重复页或达到上限时停止。weread.mobile_max_pages 可配置为 1～10。手动回补每次两页、每页 5 条。按 reviewId 去重，保留接口实际发布时间；不能据此保证覆盖公众号全部历史。

移动端未授权、临时冷却或列表失败时保留上游 Web 列表和最新单篇兼容流程；错误单独记录。上游的正文解析更新仍保留，包括 content_noencode 文本格式。后台沿用原正文任务和失败计数，页面外壳会记为失败并继续其他文章。

## 登录状态与自动续期

管理页每 30 秒刷新状态。移动端使用已有 refreshToken 和同一 deviceId；Web 使用独立 wr_rt，续期响应必须包含新 Cookie，并验证有效性后才保存。兼容上游 weread_data.accounts，按当前账号保存 Cookie，不覆盖其他账号。

服务启动后每 30 分钟维护检查，正常距上次续期 12 小时后尝试续期。明确的认证失效触发一次续期和一次请求重试；续期失败退避 30 分钟。限流、验证要求单独停止，不当作空列表成功。Windows 和 Linux 使用系统文件锁串行化令牌轮换。

移动端新令牌先原子写入 data/weread_mobile.json，再执行正常 mobileSync；同步失败不丢失已轮换令牌。Web 继续使用 data/wx.lic（或现有 weread.cookie 配置来源）。凭据不出现在新状态接口响应和续期日志里。

平台撤销授权、续期信息失效或要求人工确认时仍需重新扫码；页面显示两套授权的错误及 needs_reauth。没有配置外部消息推送。账号文件与数据库属于本机运行数据，不能提交到 Git。

## 接口与兼容

状态、扫码和历史操作复用 WeRSS JWT 登录：
- GET /api/v1/wx/weread/mobile/status
- POST /api/v1/wx/weread/mobile/qr/code
- POST /api/v1/wx/weread/mobile/verify
- POST /api/v1/wx/weread/mobile/collect

原 /api/v1/wx/weread/mobile/panel 书签跳转到 /weread。管理功能由原生 Vue/Arco 组件整合，不依赖独立页面或 iframe。

协议适配参考 x554960766/wechat-mp-tools 的 auth.py、articles.py、weread_signer.py；其 MIT 许可证保留在 docs/licenses/wechat-mp-tools-MIT.txt。上游接口可能变化，实际可返回的文章窗口与正文以当次请求为准。

## 构建与验证

在项目根目录安装后端依赖并准备 config.yaml 后运行：

    python -m unittest discover -s tests -p 'test_weread_*.py' -q
    python tests/verify_weread_new_subscription.py

新增公众号验证使用全新临时 SQLite 和虚构授权、模拟两页响应，调用真实 add_mp 与排队采集方法；不读取生产数据库或真实授权，不发送上游采集请求。

前端在 web_ui 目录执行 npm ci 和 npm run build。本仓库已同步编译产物到 static。后续修改前端后，先将 web_ui/dist 的内容同步到根目录 static，再构建 Docker 镜像；Dockerfile 不执行前端编译。
