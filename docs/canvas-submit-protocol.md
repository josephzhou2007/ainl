# Canvas 文件提交协议（实测抓包，2026-09-15）

来源：PHYSICS 247 → Problem Set 1，seq 3615 / 3617 / 3619。三步，全部 2xx。
与 Canvas 官方文档的 "Uploading Files" 三步流程一致，可以直接用官方 REST 等价实现。

## 实测（浏览器内部路径）

```
① POST https://canvas.wisc.edu/files/pending
   Content-Type: application/x-www-form-urlencoded
   name=<文件名>&on_duplicate=rename&no_redirect=true
   &attachment[intent]=submit
   &attachment[asset_string]=assignment_<ASSIGNMENT_ID>
   &attachment[filename]=<文件名>
   → 200 { file_param:"file", upload_url:"https://inst-fs-iad-prod.inscloudgate.net/files?token=<JWT>", ... }

② POST <upload_url>            （multipart/form-data，字段名 = file_param）
   → 201 { id:<FILE_ID>, instfs_uuid, location:"https://canvas.wisc.edu/api/v1/files/<FILE_ID>?..." }

③ POST https://canvas.wisc.edu/courses/<COURSE_ID>/assignments/<ASSIGNMENT_ID>/submissions
   Content-Type: application/x-www-form-urlencoded
   authenticity_token=<CSRF>
   &submission[submission_type]=online_upload
   &submission[attachment_ids][]=<FILE_ID>
   → 201 { submission:{ assignment_id, user_id, submission_type:"online_upload", submitted_at, ... } }
```

## 官方 REST 等价（L7-A 用这个，不用上面的内部路径）

```
① POST /api/v1/courses/:course_id/assignments/:assignment_id/submissions/self/files
     name=<文件名>&size=<bytes>&content_type=application/pdf
   → { upload_url, upload_params }
② POST <upload_url>  multipart: 先放 upload_params 的每个字段，最后放 file
   → { id, ... }
③ POST /api/v1/courses/:course_id/assignments/:assignment_id/submissions
     submission[submission_type]=online_upload
     submission[file_ids][]=<id>
   → Submission 对象
```

官方路径用 Bearer token，没有 CSRF（authenticity_token 只在浏览器 session 路径需要）。
`upload_url` 带的 JWT 有效期短（分钟级），①②之间不要拖。

## 幂等 / 安全

- ③ 对同一 assignment 重复调用 = 重新提交（Canvas 保留历史版本），不会报错，但要在 UI 上告知用户"这是第 N 次提交"。
- ① 之前用 `GET /api/v1/courses/:id/assignments/:aid` 检查 `submission_types` 含 `online_upload`，且 `locked_for_user == false`。
- 上传后回读 `GET .../submissions/self`，把 `submitted_at` 和 `attachments[].display_name` 回显给用户作为回执。
