# 教授访谈清单（每人 5 分钟）

目的不是做用户研究，是**填路由表**。你只需要问出足够生成 route YAML 的信息。
如果对方愿意多聊，再问 B 组。

开场（一句话，别说"我在做一个替代 Canvas 的产品"，会引起防御）：
> "我在做一个帮学生整理各科 deadline 和材料的小工具，想确认一下您这门课的流程，
>  这样我不会搞错。能占您五分钟吗？"

---

## A 组｜必问（决定 route YAML，缺一不可）

1. **作业在哪交？** Canvas 本身，还是 Gradescope / McGraw Hill / 别的？
   - 如果是外部：Canvas 上会同步显示 deadline 吗，还是只在那边能看到？
2. **新作业发布时，学生怎么知道？** Canvas 通知 / 课上口头说 / 邮件 / 都有？
3. **课程材料（PDF、讲义、课本）放在哪？** Files / Modules / Pages / 外部链接？
   - 会中途更新吗？更新了学生怎么知道？
4. **答疑走哪里？** Piazza / 邮件 / office hour / Canvas Inbox？
5. **Canvas 上哪些标签是您实际在用的，哪些是学校默认挂着但您根本不用的？**
   （这条价值极高——SPANISH 101 挂了 12 个标签，大概率大半是死的）

## B 组｜有时间再问（产品论据）

6. 您每学期花多少时间**配置** Canvas？哪一步最烦？
7. 有没有学生因为"没找到"而漏交作业？大概多久一次？
8. 您给学生设 due date 的时候，`11:59pm` 和"上课那天"是同一个意思吗？
   （已知数据：SPANISH 101 有 8 个作业时间戳完全相同 = 上课时间，不是真死线）
9. 如果有个工具能在 deadline 前自动提醒学生、并把材料推给他们，
   您会担心什么？（听反对意见，比听赞美有用）

## C 组｜如果对方感兴趣，再抛

10. 您愿意让我录一下您第一节课讲"本课怎么用 Canvas"那段吗？
    （明确说明：只用于生成这门课的导航规则，不公开、不外传）

---

## 记录方式

**别当场记笔记**，会打断节奏。用手机录音（先征得同意），事后转写。
每个教授录完存成 `research/raw/<课程代码>-<日期>.m4a`。

## 回来后填这个

每门课一份，放 `routes/<课程代码>.yaml`：

```yaml
course: MATH222
canvas_id: 100003
homework:   { where: gradescope,  deadline_visible_in_canvas: true }
materials:  { where: canvas_files, updates_announced: false }
qa:         { where: piazza }
exams:      { where: registerblast }
dead_tabs:  [Course Analytics, ...]   # 挂着但没人用的
notes: |
    教授原话里值得记的
```
