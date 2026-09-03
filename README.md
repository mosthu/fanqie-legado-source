# fanqie-legado-source

一个面向 Legado（阅读）的实验性番茄小说公开网页版适配服务。

目标：把番茄公开网页中正常可访问的搜索、书籍详情、目录和章节内容整理为简单 JSON，供 Legado 书源调用。

边界：
- 只处理公开网页正常提供的数据。
- 不绕过登录、会员、付费或访问控制。
- 不破解字体混淆/字符加密；若正文存在私用区字符，会返回 `readable=false`。

## 已部署服务

- Base URL: `https://fanqie-legado-source.onrender.com`
- 健康检查: `https://fanqie-legado-source.onrender.com/health`
- 搜索测试: `https://fanqie-legado-source.onrender.com/search?q=神通者`

Render 免费实例在长时间无访问后会休眠，首次请求可能需要几十秒唤醒。

搜索请求有 35 秒总超时，并在响应中附带 `requestId` 和分阶段诊断信息。若番茄公开搜索接口要求滑块等交互式验证，服务会返回明确的 `503 fanqie_public_search_verification_required`，不会尝试绕过；若页面一直停留在加载状态，则返回 `504 fanqie_search_results_timeout`，不会无限挂起。

## Legado 书源

已生成可直接导入的书源：

- `legado/fanqie-public-v1.0.json`

也可以使用模板：

- `legado/source.template.json`

## API

- `GET /health`
- `GET /search?q=神通者`
- `GET /info?url=https://fanqienovel.com/page/...`
- `GET /content?url=https://fanqienovel.com/reader/...`

## 本地运行

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t fanqie-legado-source .
docker run --rm -p 8000:8000 fanqie-legado-source
```
