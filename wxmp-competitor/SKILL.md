---
name: wxmp-competitor
description: |
  微信公众号竞品监控与分析。从 wxdown (wechat-article-exporter) 拉取竞品公众号最新文章，
  分析发布频率、标题策略、内容主题、热门话题，生成竞品日报/周报。
  触发条件：用户要求分析竞品、查看竞品动态、生成竞品报告，或定时任务触发。
  关键词：竞品分析、竞品日报、竞品动态、对标分析、竞品周报
---

# 微信公众号竞品监控与分析

自动监控竞品公众号，分析其内容策略，生成结构化报告。

## 数据来源

文章数据来自 wxdown / wechat-article-exporter（本地 Docker，port 8067）。
竞品公众号列表来自 wxmp-wxdown skill 的 follows.json。
需要先通过 wxmp-wxdown skill 搜索并关注竞品号。

## 用法

### 生成竞品日报
```bash
python3 scripts/competitor-analysis.py --daily
```

### 生成竞品周报
```bash
python3 scripts/competitor-analysis.py --weekly
```

### 分析指定公众号
```bash
python3 scripts/competitor-analysis.py --account "量子位"
```

### 查看所有监控的公众号
```bash
python3 scripts/competitor-analysis.py --list
```

## 输出格式

Markdown 格式的竞品分析报告，包含：
- 各竞品近期发文概览（标题、时间）
- 发布频率统计
- 标题关键词/热词分析
- 内容主题分类
- 值得借鉴的选题和角度

## 监控账号

| 类型 | 公众号 |
|------|--------|
| 头部媒体 | 量子位、机器之心、新智元 |
| 个人号 | 卡兹克、findyi、汤师爷 |

## 依赖

- python3（标准库即可，不需要 requests）
- wxdown 容器运行中 (port 8067)
- wxmp-wxdown skill 的 follows.json（关注列表）
