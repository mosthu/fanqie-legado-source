# fanqie-legado-source

一个面向 Legado（阅读）的实验性番茄小说公开网页版适配服务。

目标：把番茄公开网页中正常可访问的搜索、书籍详情、目录和章节内容整理为简单 JSON，供 Legado 书源调用。

边界：
- 只处理公开网页正常提供的数据。
- 不绕过登录、会员、付费或访问控制。
- 不破解字体混淆/字符加密；若正文存在私用区字符，会返回 `readable=false`。

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

部署后，把 `legado/source.template.json` 里的 `__BASE_URL__` 替换成你的服务地址即可导入阅读。
