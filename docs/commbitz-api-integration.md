# Commbitz 分销商 API 接入指南（中文整理）

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 原文标题 | Distributor API Integration Guide |
| 供应商 | Commbitz |
| 原文版本 | Version 2.0 · May 2026 |
| 原文变更日期 | 2026-05-26 |
| 原文页数 | 25 页 |
| 来源文件 | `_api_integration_guide 2 (1).pdf` |
| 原文保密标记 | Confidential；For authorised distributors only — do not redistribute |
| 原文版权 | © 2026 Commbitz |

本文按原文结构整理为中文，保留接口路径、字段名、枚举大小写、响应层级、示例与错误消息。示例中的凭据、套餐、订单、证件链接均为原文示例，不代表当前账户实际可用的数据。

第 1–13 节为原文内容整理；第 14 节为整理过程中发现的差异和项目接入事项；第 15 节记录实际只读联调结果；第 16 节补充公开 Swagger 的当前接口定义。原文协议、项目需求和实测数据分别标注，联调记录不能视为供应商对未验证能力的承诺。

## 目录

1. [基础地址与认证约定](#1-基础地址与认证约定)
2. [整体接入流程](#2-整体接入流程)
3. [获取和刷新令牌](#3-获取和刷新令牌)
4. [按分销商账户启用的强制 KYC](#4-按分销商账户启用的强制-kyc)
5. [目录与套餐查询](#5-目录与套餐查询)
6. [统一提交业务请求](#6-统一提交业务请求)
7. [查询订单或业务请求详情](#7-查询订单或业务请求详情)
8. [INR 账户订单的 KYC 流程](#8-inr-账户订单的-kyc-流程)
9. [eSIM 用量查询](#9-esim-用量查询)
10. [完整接口清单](#10-完整接口清单)
11. [通用错误处理](#11-通用错误处理)
12. [原文接入检查清单](#12-原文接入检查清单)
13. [原文变更记录](#13-原文变更记录)
14. [原文差异与待确认事项](#14-原文差异与待确认事项)
15. [实际只读联调记录](#15-实际只读联调记录)
16. [公开 Swagger 与开发补充](#16-公开-swagger-与开发补充)

## 1. 基础地址与认证约定

> 对应原文第 3 页。

### 1.1 环境地址

| 环境 | API 基础地址 | Swagger UI |
| --- | --- | --- |
| UAT | `https://api-uat.commbitz.com/distributor-api` | [UAT Swagger](https://api-uat.commbitz.com/api/api-distributor) |
| Live | `https://api-cb.commbitz.com/distributor-api` | [Live Swagger](https://api-cb.commbitz.com/api/api-distributor) |

下文 `{baseUrl}` 已包含 `/distributor-api` 前缀。例如：

```text
{baseUrl}/v1/get-token
= https://api-uat.commbitz.com/distributor-api/v1/get-token
```

### 1.2 请求格式

- 大多数请求使用 `Content-Type: application/json`。
- `POST /v1/request` 上传 KYC 文件时使用 `multipart/form-data`。
- `POST /v1/orders/:orderId/kyc-documents` 支持 JSON 证件 URL 或 multipart 文件上传。

### 1.3 鉴权范围

| 接口 | 是否需要 Bearer | 请求体中的认证信息 |
| --- | --- | --- |
| `POST /v1/get-token` | 否 | `apiKey`、`secretKey` |
| `POST /v1/refresh-token` | 否 | `refreshToken` |
| 其他所有接口 | 是 | 按业务接口填写 |

受保护接口使用以下请求头：

```http
Authorization: Bearer <access_token>
```

### 1.4 令牌有效期

| 令牌 | 有效期 | 用途 |
| --- | --- | --- |
| Access token | 1 小时，即 3,600 秒 | 业务接口的 `Authorization` 请求头 |
| Refresh token | 7 天 | 换取新的 access token，无需重新发送 API Key/Secret |

## 2. 整体接入流程

> 对应原文第 4 页。

```text
1. POST /v1/get-token
   获取 accessToken、refreshToken

2. 查询目录与套餐
   GET /v1/regional-plan-types
   GET /v1/countries
   GET /v1/plans
   GET /v1/plans/:id

3. POST /v1/request
   提交激活、充值、兑换券、eSIM 或实体 SIM 请求
   支持 JSON / multipart/form-data
   可附带 hoiUserId、kycUserInfo

   INR 账户，无证件：kycStatus = "pending"
   INR 账户，有证件：kycStatus = "submitted"
   非 INR 账户：kycStatus = null（不代表账户级强制 KYC 一定关闭）

4. GET /v1/details/:id
   查询完整订单或业务请求详情
   eSIM 数据位于 esims[]，按购买数量返回多条记录

5. INR 订单仍待提交证件时
   POST /v1/orders/:orderId/kyc-documents
   上传证件；响应可包含 esims[]，仍需注意审核状态

6. 可选：GET /esim/usage
   查询 eSIM 用量
```

Commbitz 还可以单独为某个分销商账户开启强制 KYC。该配置与 INR 账户自动 KYC 是两套规则；启用后，`activation`、`esim` 请求需要附带证件。

## 3. 获取和刷新令牌

> 对应原文第 5–6 页。

### 3.1 获取令牌

```http
POST {baseUrl}/v1/get-token
Content-Type: application/json
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `apiKey` | string | 是 | Commbitz 提供的分销商 API Key |
| `secretKey` | string | 是 | Commbitz 提供的分销商 Secret Key |

请求示例：

```json
{
  "apiKey": "your-api-key",
  "secretKey": "your-secret-key"
}
```

成功响应，HTTP `201`：

```json
{
  "message": "Tokens generated successfully",
  "statusCode": 201,
  "data": {
    "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "expiresIn": 3600
  }
}
```

凭据错误，HTTP `401`：

```json
{
  "statusCode": 401,
  "message": "Invalid API credentials"
}
```

### 3.2 刷新令牌

```http
POST {baseUrl}/v1/refresh-token
Content-Type: application/json
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `refreshToken` | string | 是 | 获取令牌接口返回的 refresh token |

请求示例：

```json
{
  "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

成功响应，HTTP `200`：

```json
{
  "message": "Token refreshed successfully",
  "statusCode": 200,
  "data": {
    "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "expiresIn": 3600
  }
}
```

原文建议在 access token 到期前刷新，例如每 50 分钟刷新一次。

## 4. 按分销商账户启用的强制 KYC

> 对应原文第 7 页；与第 8 节的 INR 订单 KYC 分开理解。

KYC 指身份核验。Commbitz 为分销商账户开启强制 KYC 后，`activation` 和 `esim` 请求必须提供证件。

### 4.1 通过 JSON 提交证件 URL

在 `POST /v1/request` 请求体中提供 `kycDocuments`：

```json
{
  "requestType": "activation",
  "sku": "US-ESIM-1GB-7D",
  "iccid": "8901260123456789012",
  "kycDocuments": {
    "passportFront": "https://cdn.example.com/kyc/passport-front.jpg",
    "passportBack": "https://cdn.example.com/kyc/passport-back.jpg",
    "visaFront": "https://cdn.example.com/kyc/visa-front.jpg"
  }
}
```

| 字段 | 含义 | URL 要求 |
| --- | --- | --- |
| `passportFront` | 护照正面 | HTTPS |
| `passportBack` | 护照背面 | HTTPS |
| `visaFront` | 签证正面 | HTTPS |

未提供强制要求的证件时，HTTP `400`：

```json
{
  "statusCode": 400,
  "message": "kycDocuments is mandatory for this distributor when creating activation or eSIM order"
}
```

### 4.2 通过 multipart 上传文件

仍使用 `POST {baseUrl}/v1/request`，提供业务表单字段，并可附带最多三个文件字段：

| 文件字段 | 类型 | 处理方式 |
| --- | --- | --- |
| `passportFront` | file | 上传到 S3，URL 保存到 `kycDocuments.passportFront` |
| `passportBack` | file | 上传到 S3 |
| `visaFront` | file | 上传到 S3 |

允许的文件类型：PDF、JPEG、PNG、GIF、WebP。

原文 cURL 示例：

```bash
curl -X POST "https://api-cb.commbitz.com/distributor-api/v1/request" \
  -H "Authorization: Bearer <access_token>" \
  -F "requestType=activation" \
  -F "sku=US-ESIM-1GB-7D" \
  -F "iccid=8901260123456789012" \
  -F "passportFront=@/path/to/passport-front.jpg" \
  -F "passportBack=@/path/to/passport-back.jpg" \
  -F "visaFront=@/path/to/visa-front.jpg"
```

本节原文没有给出单文件大小上限，也没有明确账户级强制 KYC 是否必须同时提供三份文件。第 8 节 INR 补交证件接口另有“至少一份”的要求。

## 5. 目录与套餐查询

> 对应原文第 8–11 页；所有接口均需要 Bearer token。

### 5.1 查询区域套餐类型

```http
GET {baseUrl}/v1/regional-plan-types
```

只返回与当前分销商已分配套餐关联的区域套餐类型。

可选查询参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `regionId` | string（ObjectId） | 按区域 ID 过滤 |
| `regionName` | string | 区域名称模糊搜索，不区分大小写 |

成功响应，HTTP `200`：

```json
{
  "message": "Regional plan types retrieved successfully",
  "statusCode": 200,
  "data": {
    "regionalPlanTypes": [
      {
        "_id": "65d33654136c54ab64b20a7a",
        "name": "Europe",
        "description": "European region plans"
      },
      {
        "_id": "65d33654136c54ab64b20a7b",
        "name": "Asia",
        "description": "Asia region plans"
      }
    ],
    "count": 2
  }
}
```

返回的 `_id` 可作为 `GET /v1/plans` 的 `regionalPlanTypeId`。

### 5.2 查询国家

```http
GET {baseUrl}/v1/countries
```

原文响应示例：

```json
{
  "message": "Countries retrieved successfully",
  "statusCode": 200,
  "data": {
    "countries": [
      {
        "_id": "65d33654136c54ab64b20a7a",
        "name": "United States",
        "iso": "US",
        "flagImageUrl": "https://example.com/flags/us.png",
        "dialCode": "+1",
        "eSimSupported": true
      }
    ],
    "count": 195
  }
}
```

返回的 `_id` 可作为 `GET /v1/plans` 的 `countryId`。原文示例只展开一个国家，`count` 表示示例中的总数。

### 5.3 查询已分配套餐

```http
GET {baseUrl}/v1/plans
```

所有查询参数均为可选：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `simCategory` | string | `esim` 或 `physicalsim` |
| `countryId` | string（ObjectId） | 来自 `/v1/countries` 的国家 ID |
| `regionalPlanTypeId` | string（ObjectId） | 来自 `/v1/regional-plan-types` 的区域 ID |
| `search` | string | 按套餐名称或 SKU 搜索，不区分大小写 |
| `planIsFor` | number | 适用业务类型，见下表 |
| `page` | number | 默认 `1` |
| `limit` | number | 默认 `10`，最大 `100` |

`planIsFor` 枚举：

| 值 | 原文含义 | 中文说明 |
| --- | --- | --- |
| `0` | Only Activation | 仅激活 |
| `1` | Only Recharge | 仅充值 |
| `2` | Only Order | 仅下单 |
| `3` | Show All | 展示全部 |
| `4` | Activation and Recharge | 激活与充值 |
| `5` | Activation and Order | 激活与下单 |

成功响应，HTTP `200`：

```json
{
  "message": "Plans retrieved successfully",
  "statusCode": 200,
  "data": {
    "plans": [
      {
        "_id": "68e8bfea8d74931d8e09aa6d",
        "name": "eSIM - United States - 1GB - 7 Days",
        "sku": "US-ESIM-1GB-7D",
        "dataAmount": 1024,
        "duration": 7,
        "simCategory": "esim",
        "brandId": ["686f656fee4b8497a5dd842e"],
        "brand": { "name": "Brand Name" },
        "country": {
          "_id": "...",
          "name": "United States",
          "iso": "US"
        },
        "planIsFor": 3,
        "pricing": {
          "activationPrice": 5.00,
          "rechargePrice": 4.50,
          "retailPrice": 6.00,
          "overridePrice": 5.00,
          "currency": {
            "_id": "...",
            "code": "USD",
            "symbol": "$"
          }
        },
        "distributorPlanId": "68e8b65a5e36b4e18ff6e823",
        "planName": "US 1GB 7 Days"
      }
    ],
    "pagination": {
      "total": 100,
      "page": 1,
      "limit": 10,
      "totalPages": 10,
      "hasNextPage": true,
      "hasPrevPage": false
    }
  }
}
```

字段用途：

- 使用 `sku` 调用 `POST /v1/request`。
- 使用套餐 `_id` 调用 `GET /v1/plans/:id`。
- `pricing.currency` 描述价格币种；原文没有给出多个价格字段的最终扣款优先级。

### 5.4 查询单个套餐详情

```http
GET {baseUrl}/v1/plans/:id
```

| 路径参数 | 说明 |
| --- | --- |
| `:id` | `/v1/plans` 返回的套餐 `_id` |

返回面向用户的完整套餐规格，以及分销商专属价格。

成功响应，HTTP `200`：

```json
{
  "success": true,
  "message": "Plan details retrieved successfully",
  "data": {
    "_id": "68e8bfea8d74931d8e09aa6d",
    "name": "eSIM - United States - 1GB - 7 Days",
    "sku": "US-ESIM-1GB-7D",
    "simCategory": "esim",
    "brand": {
      "_id": "...",
      "name": "Brand Name"
    },
    "data": {
      "amount": 1024,
      "unit": "MB",
      "label": "1 GB",
      "unlimited": false,
      "perDay": false
    },
    "validity": { "duration": 7 },
    "coverage": {
      "country": {
        "_id": "...",
        "name": "United States",
        "iso": "US",
        "flagImageUrl": "..."
      },
      "coverageCountries": [],
      "bundleType": 0
    },
    "features": {
      "voice": false,
      "text": false,
      "sms": false,
      "hotspotTethering": true
    },
    "network": {
      "type": "4G",
      "speed": ["4G", "LTE"]
    },
    "planIsFor": 3,
    "autoStart": false,
    "autoRenew": false,
    "description": "1GB data for 7 days in the United States.",
    "pricing": {
      "activationPrice": 5.00,
      "rechargePrice": 4.50,
      "retailPrice": 6.00,
      "overridePrice": 5.00,
      "currency": {
        "code": "USD",
        "symbol": "$"
      }
    }
  }
}
```

错误：HTTP `404`，套餐不存在或未分配给当前账户。原文没有给出该错误的完整 JSON 示例。

这里的 `data.data` 是套餐流量规格，不是订单接口的第二层响应包装。

## 6. 统一提交业务请求

> 对应原文第 12–15 页。

```http
POST {baseUrl}/v1/request
```

支持 `application/json` 和 `multipart/form-data`。

### 6.1 通用请求字段

| 字段 | 类型 | 必填性（通用字段表） | 说明 |
| --- | --- | --- | --- |
| `requestType` | string | 是 | `activation`、`recharge`、`voucher`、`esim`、`physical` |
| `sku` | string | 是 | 来自 `/v1/plans` 的 SKU |
| `iccid` | string | 条件必填 | eSIM 激活时要激活的 ICCID |
| `mobile_number` | string | 条件必填 | 充值目标手机号 / MSISDN |
| `quantity` | number | 条件必填 | eSIM / 实体 SIM 数量，通用表写默认 `1`；兑换券可选 |
| `days` | number | 否 | 按日套餐天数，默认 `1`；原文公式为 `totalAmount = unitPrice × days` |
| `hoiUserId` | string | 否 | 接入方内部最终用户标识；保存为订单的 `hoiUserId`，如 `USR-20482` |
| `kycUserInfo` | object | 否 | 最终用户资料，保存在订单中 |
| `kycDocuments` | object | 条件必填 | `passportFront`、`passportBack`、`visaFront` 的 HTTPS URL；账户开启强制 KYC 时，激活/eSIM 需要提供 |
| `planId` | string（ObjectId） | 否 | 已知的套餐 ID |
| `brandId` | string（ObjectId） | 否 | 已知的品牌 ID |
| `notes` | string | 否 | 自由文本备注 |

### 6.2 `kycUserInfo` 子字段

原文将以下字段全部列为可选：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 最终用户全名 |
| `email` | string | 电子邮件地址 |
| `phoneNumber` | string | 手机号 |
| `travelDate` | string（ISO） | 出行日期，如 `2026-06-01` |
| `returnDate` | string（ISO） | 返回日期 |
| `passportExpiryDate` | string（ISO） | 护照到期日 |
| `address` | string | 完整地址 |
| `city` | string | 城市 |
| `zipCode` | string | 邮政编码 |
| `state` | string | 州 / 省 |
| `country` | string | 国家 |

### 6.3 按请求类型划分的字段表

下表保留原文第 13 页的 Required / Optional 划分。它与通用字段表、强制 KYC 说明存在差异，见第 14 节。

| `requestType` | 原文 Required | 原文 Optional |
| --- | --- | --- |
| `activation` | `requestType`、`sku` | `iccid`、`planId`、`brandId`、`notes`、`hoiUserId`、`kycUserInfo`、`kycDocuments`（KYC 开启时） |
| `recharge` | `requestType`、`sku` | `mobile_number` 或 `iccid`、`days`、`planId`、`brandId`、`notes`、`hoiUserId`、`kycUserInfo` |
| `voucher` | `requestType`、`sku` | `quantity`、`planId`、`brandId`、`notes`、`hoiUserId` |
| `esim` | `requestType`、`sku`、`quantity` | `planId`、`brandId`、`notes`、`hoiUserId`、`kycUserInfo`、`kycDocuments` |
| `physical` | `requestType`、`sku`、`quantity` | `planId`、`brandId`、`notes`、`hoiUserId`、`kycUserInfo` |

### 6.4 请求示例

#### eSIM 新购，附带用户资料

```json
{
  "requestType": "esim",
  "sku": "US-ESIM-1GB-7D",
  "quantity": 4,
  "hoiUserId": "USR-20482",
  "kycUserInfo": {
    "name": "John Doe",
    "email": "john@example.com",
    "phoneNumber": "+919876543210",
    "travelDate": "2026-06-01",
    "returnDate": "2026-06-15"
  }
}
```

#### eSIM 激活

```json
{
  "requestType": "activation",
  "sku": "US-ESIM-1GB-7D",
  "iccid": "8901260123456789012"
}
```

#### 充值

```json
{
  "requestType": "recharge",
  "sku": "US-ESIM-1GB-7D",
  "mobile_number": "+919876543210"
}
```

#### 兑换券

```json
{
  "requestType": "voucher",
  "sku": "US-VOUCHER-1GB"
}
```

#### 实体 SIM 下单

```json
{
  "requestType": "physical",
  "sku": "US-PHYS-1D",
  "quantity": 10,
  "hoiUserId": "USR-30912"
}
```

### 6.5 激活 / 充值 / 兑换券的成功响应

HTTP `201`；原文统一以激活请求示例展示：

```json
{
  "statusCode": 201,
  "data": {
    "success": true,
    "message": "Activation request processed successfully",
    "data": {
      "_id": "693ae27c038c06028026d485",
      "orderId": "AR06512",
      "brandActivationRequestId": "AR06512",
      "transactionId": "TXN66747712283",
      "status": "pending",
      "requestType": "activation",
      "plan": {
        "_id": "68e8b65a5e36b4e18ff6e823",
        "name": "...",
        "sku": "US-ESIM-1GB-7D"
      },
      "iccid": "8901260123456789012",
      "phoneNumber": "+919876543210"
    }
  }
}
```

### 6.6 eSIM / 实体 SIM 下单的成功响应

HTTP `201`：

```json
{
  "statusCode": 201,
  "data": {
    "success": true,
    "message": "eSIM order created successfully",
    "data": {
      "_id": "6983b3e4c91d14265c8874e9",
      "status": "Success",
      "requestType": "esim",
      "quantity": 4,
      "kycStatus": null,
      "isKycRequired": false,
      "isKycVerified": false,
      "order": { "...": "..." },
      "currency": {
        "code": "USD",
        "symbol": "$"
      }
    }
  }
}
```

重要字段：

- 保存 `data.data._id`，后续使用该值调用 `GET /v1/details/:id`。
- 不要把业务编号 `orderId` 或 `brandActivationRequestId` 当作详情接口要求的 `_id`。
- INR 账户的 `kycStatus`：没有证件时为 `pending`，已有证件时为 `submitted`。
- `status` 示例保留原文大小写：这里为 `Success`，其他接口示例还有 `pending`、`approved`。

错误示例：

| HTTP 状态 | 原文消息 |
| --- | --- |
| `400` | `requestType is required` |
| `400` | `kycDocuments is mandatory for this distributor when creating activation or eSIM order` |
| `404` | `Plan not found with SKU: INVALID-SKU` |

## 7. 查询订单或业务请求详情

> 对应原文第 16–17 页。

```http
GET {baseUrl}/v1/details/:id
```

| 路径参数 | 说明 |
| --- | --- |
| `:id` | `POST /v1/request` 返回的 `_id` |

### 7.1 eSIM / 实体 SIM 订单详情

原文成功响应示例：

```json
{
  "statusCode": 200,
  "data": {
    "success": true,
    "message": "Order details retrieved successfully",
    "data": {
      "_id": "6983b3e4c91d14265c8874e9",
      "orderId": "DR00658",
      "planId": "68e8b65a5e36b4e18ff6e823",
      "brandId": "686f656fee4b8497a5dd842e",
      "totalAmount": 500,
      "quantity": 4,
      "kycStatus": null,
      "isKycRequired": false,
      "isKycVerified": false,
      "esims": [
        {
          "_id": "...",
          "distributorEsimId": "DESIM000011748234567890",
          "iccid": "896604312530099781",
          "qrCode": "https://bucket.s3.region.amazonaws.com/xxx-qr-123.png",
          "lpa": "LPA:1$rsp-3090.idemia.io$GGCLM-ISLLD-ETMNC-L4MAI",
          "smdpAddress": "rsp-3090.idemia.io",
          "msisdn": "66658649412",
          "status": "active",
          "salePlanName": "US 1GB 7 Days",
          "salePlanDays": 7,
          "activationDate": null,
          "expiryDate": null,
          "vendor": 1,
          "isFromInventory": false,
          "createdAt": "2026-05-26T04:20:00.000Z"
        }
      ]
    }
  }
}
```

原文说明：

- `esims` 始终为数组，不是单个对象。
- 每张生成的 eSIM 对应一条数组记录；数量为 `4` 时，应返回 `4` 条记录。
- 原文 JSON 示例仅展开一条记录，不能据此将实际数量固定为 `1`。
- INR 订单处于 `kycStatus: "pending"` 时，`esims` 为空，直到提交证件。

`esims[]` 示例字段：

| 字段 | 含义 / 原文信息 |
| --- | --- |
| `_id` | eSIM 记录 ID |
| `distributorEsimId` | 分销商 eSIM 编号 |
| `iccid` | SIM 标识 |
| `qrCode` | 二维码图片 URL |
| `lpa` | eSIM LPA 安装字符串 |
| `smdpAddress` | SM-DP+ 地址 |
| `msisdn` | 号码；部分响应示例为 `null` |
| `status` | eSIM 状态，示例为 `active` |
| `salePlanName` | 销售套餐名称 |
| `salePlanDays` | 套餐天数 |
| `activationDate` | 激活时间；示例为 `null` |
| `expiryDate` | 到期时间；示例为 `null` |
| `vendor` | 供应商标识，原文仅给出数值示例，未给出枚举 |
| `isFromInventory` | 是否来自库存 |
| `createdAt` | 创建时间 |

变更记录还列出 `simplyActivationCode`、`snPin`、`snCode`、`configuration_pin`，但未提供其完整类型、必填性或格式说明。

### 7.2 激活 / 充值 / 兑换券请求详情

原文成功响应示例：

```json
{
  "statusCode": 200,
  "data": {
    "success": true,
    "message": "Activation request details retrieved successfully",
    "data": {
      "_id": "693ae27c038c06028026d485",
      "orderId": "AR06512",
      "transactionId": "TXN66747712283",
      "status": "approved",
      "requestType": "activation",
      "iccid": "896604312530099781",
      "phoneNumber": "+919876543210",
      "voucher": "VOUCHER123",
      "msisdn": "+1234567890"
    }
  }
}
```

原文没有分别展开三类业务的完整响应差异，不能据此认定所有字段在每种业务中均存在。

错误：

| HTTP 状态 | 原文消息 |
| --- | --- |
| `404` | `Order or activation request not found or does not belong to the distributor` |
| `400` | `Invalid ID format` |

## 8. INR 账户订单的 KYC 流程

> 对应原文第 18–20 页。

原文规定：分销商账户结算币种为 INR 时，此流程自动生效，不需要额外配置。第 25 页变更记录将 INR 自动 KYC 的适用范围描述为 eSIM 和实体 SIM 订单。

### 8.1 场景与状态

| 账户 / 场景 | 下单是否附带 `kycDocuments` | 结果 |
| --- | --- | --- |
| INR 账户，下单时已有证件 | 是 | `kycStatus: "submitted"`、`isKycRequired: true`；订单创建成功 |
| INR 账户，下单时没有证件 | 否 | `kycStatus: "pending"`、`isKycRequired: true`；订单已创建，但 eSIM 尚未发放 |
| 非 INR 账户 | 任意 | 本流程下 `kycStatus: null`、`isKycRequired: false`；仍需另行考虑账户级强制 KYC |

原文 KYC 状态流程：

```text
INR 订单创建
  |
  +-- 已附证件 --> submitted --> CRM 管理员审核
  |                                |
  |                                +--> verified / isKycVerified = true --> eSIM released
  |
  +-- 未附证件 --> pending --> POST /v1/orders/:orderId/kyc-documents
                                 |
                                 +--> submitted，响应可返回 esims[]
                                         |
                                         +--> CRM 管理员审核
                                                 |
                                                 +--> verified / isKycVerified = true --> eSIM released
```

原文同时描述“提交证件即可返回 `esims[]`”与“管理员审核后释放 eSIM”。两者不是同一个状态；交付时机需确认，详见第 14 节。

### 8.2 创建暂未附证件的 INR 订单

```json
{
  "requestType": "esim",
  "sku": "IN-ESIM-1GB-7D",
  "quantity": 2,
  "hoiUserId": "USR-20482",
  "kycUserInfo": {
    "name": "Rahul Sharma",
    "email": "rahul@example.com",
    "phoneNumber": "+919876543210",
    "travelDate": "2026-06-01",
    "passportExpiryDate": "2030-01-01"
  }
}
```

原文给出的响应内容节选，不代表完整外层包装：

```json
{
  "_id": "6983b3e4c91d14265c8874e9",
  "status": "pending",
  "quantity": 2,
  "kycStatus": "pending",
  "isKycRequired": true,
  "isKycVerified": false
}
```

保存 `_id`，用于补交证件。

### 8.3 为已有订单提交证件

```http
POST {baseUrl}/v1/orders/:orderId/kyc-documents
```

| 路径参数 | 说明 |
| --- | --- |
| `:orderId` | 创建订单返回的 `_id`，不是 `DR...` 业务编号 |

支持：

- `multipart/form-data`：直接上传文件，原文推荐。
- `application/json`：提交已上传到 S3 的证件 URL。

证件字段：

| 字段 | 类型 | 要求 | 说明 |
| --- | --- | --- | --- |
| `passportFront` | file / URL | 三类证件至少提供一份 | 护照正面，PDF 或图片 |
| `passportBack` | file / URL | 三类证件至少提供一份 | 护照背面，PDF 或图片 |
| `visaFront` | file / URL | 三类证件至少提供一份 | 签证正面，PDF 或图片 |

multipart 示例：

```bash
curl -X POST "https://api-cb.commbitz.com/distributor-api/v1/orders/6983b3e4c91d14265c8874e9/kyc-documents" \
  -H "Authorization: Bearer <access_token>" \
  -F "passportFront=@/path/to/passport-front.jpg" \
  -F "passportBack=@/path/to/passport-back.jpg" \
  -F "visaFront=@/path/to/visa-front.jpg"
```

JSON 示例：注意本接口的证件字段直接位于请求体顶层，不再套 `kycDocuments`。

```json
{
  "passportFront": "https://cdn.example.com/kyc/passport-front.jpg",
  "passportBack": "https://cdn.example.com/kyc/passport-back.jpg",
  "visaFront": "https://cdn.example.com/kyc/visa-front.jpg"
}
```

成功响应，HTTP `201`：

```json
{
  "success": true,
  "message": "KYC documents submitted successfully. Your order is now pending admin verification.",
  "data": {
    "orderId": "6983b3e4c91d14265c8874e9",
    "distributorOrderId": "DR00658",
    "kycStatus": "submitted",
    "isKycRequired": true,
    "isKycVerified": false,
    "kycDocuments": {
      "passportFront": "https://s3.amazonaws.com/bucket/passport-front.jpg",
      "passportBack": "https://s3.amazonaws.com/bucket/passport-back.jpg",
      "visaFront": "https://s3.amazonaws.com/bucket/visa-front.jpg"
    },
    "esims": [
      {
        "_id": "...",
        "distributorEsimId": "DESIM000011748234567890",
        "iccid": "896604312530099781",
        "qrCode": "https://bucket.s3.region.amazonaws.com/xxx-qr.png",
        "lpa": "LPA:1$rsp.example.com$ACTIVATION-CODE",
        "msisdn": null,
        "status": "active",
        "salePlanName": "IN 1GB 7 Days",
        "salePlanDays": 7
      }
    ]
  }
}
```

原文说明：本响应中的 `esims` 数组包含该订单生成的全部 eSIM，相同数据也可通过 `GET /v1/details/:id` 获取。

错误：

| HTTP 状态 | 原文消息 |
| --- | --- |
| `400` | `This order does not require KYC verification — not an INR order` |
| `400` | `KYC is already verified for this order` |
| `400` | `At least one KYC document URL must be provided` |
| `404` | `Order not found or does not belong to your account` |

## 9. eSIM 用量查询

> 对应原文第 21 页；可选能力，需要 Bearer token。

```http
GET {baseUrl}/esim/usage
```

至少提供一个查询参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `coupon` | string | eSIM coupon / `snPin` |
| `cid` | string | 上游 profile 标识 |
| `orderId` | string | 订单 ID；原文未进一步说明其具体 ID 类型 |
| `imsi` | string | IMSI |

请求示例：

```http
GET {baseUrl}/esim/usage?coupon=ysZcvthHK
```

未提供任何标识时，HTTP `400`：

```json
{
  "statusCode": 400,
  "message": "At least one of coupon, cid, orderId, or imsi is required"
}
```

原文没有提供成功响应示例、用量单位或返回字段表。

## 10. 完整接口清单

> 对应原文第 22 页。

| 序号 | 方法 | 完整路径 | 认证 | 用途 |
| --- | --- | --- | --- | --- |
| 1 | POST | `/distributor-api/v1/get-token` | API Key + Secret | 获取 access / refresh token |
| 2 | POST | `/distributor-api/v1/refresh-token` | 请求体中的 refresh token | 刷新令牌 |
| 3 | GET | `/distributor-api/v1/regional-plan-types` | Bearer | 查询已分配套餐关联的区域类型 |
| 4 | GET | `/distributor-api/v1/countries` | Bearer | 查询国家目录 |
| 5 | GET | `/distributor-api/v1/plans` | Bearer | 按条件分页查询已分配套餐与价格 |
| 6 | GET | `/distributor-api/v1/plans/:id` | Bearer | 查询完整套餐规格与价格，v2 新增 |
| 7 | POST | `/distributor-api/v1/request` | Bearer；JSON / multipart | 激活、充值、兑换券、eSIM 下单、实体 SIM 下单 |
| 8 | GET | `/distributor-api/v1/details/:id` | Bearer | 查询订单或业务请求详情 |
| 9 | POST | `/distributor-api/v1/orders/:orderId/kyc-documents` | Bearer；JSON / multipart | 为 INR 待审核订单提交证件，v2 新增 |
| 10 | GET | `/distributor-api/esim/usage` | Bearer | 查询 eSIM 用量 |

## 11. 通用错误处理

> 对应原文第 23 页。下表“处理建议”是原文建议，不代表已在本项目执行或验证。

| HTTP 状态 | 含义 | 原文处理建议 |
| --- | --- | --- |
| `401` | 令牌无效或过期 | 调用 `/v1/refresh-token` 或 `/v1/get-token`，使用新 access token 重试 |
| `404` | 资源不存在或未分配给当前账户 | 检查 ID、SKU 及套餐/订单的账户归属 |
| `400` | 请求体无效、缺少字段或未提供 KYC | 检查请求；强制 KYC 的激活/eSIM 请求需要 `kycDocuments` |
| `500` | 未预期的服务端错误 | 重试一次；仍失败则联系 Commbitz 支持 |

原文没有给出下单幂等性约定。因此，不能仅凭上述通用 `500` 建议推导出“重复提交购买不会重复扣款”，见第 14 节。

## 12. 原文接入检查清单

> 对应原文第 24 页。以下为原文建议事项，不表示已经完成配置或联调。

- 从 Commbitz 获取 API Key 和 Secret Key。
- 实现 `POST /v1/get-token`，保存 access token 和 refresh token。
- 在 access token 到期前刷新，例如每 50 分钟调用一次 `/v1/refresh-token`。
- 向 Commbitz 确认账户是否开启强制 KYC；若开启，激活/eSIM 请求附带 `kycDocuments` 或 multipart 文件。
- 查询 `/v1/plans`，按需查询区域和国家；通过 `/v1/plans/:id` 获取单个套餐完整规格。
- 创建请求时可携带 `hoiUserId` 和 `kycUserInfo`，便于追踪最终用户。
- eSIM / 实体 SIM 请求提交 `quantity`；生成的 eSIM 按数量放在 `esims[]` 中。
- INR 账户未在下单时提供证件，订单会进入 `kycStatus: "pending"`；调用订单 KYC 接口补交。
- 持久化 `POST /v1/request` 返回的 `data.data._id`，并使用 `/v1/details/:id` 轮询二维码、ICCID 和状态。

## 13. 原文变更记录

> 对应原文第 25 页；变更日期为 2026-05-26。

| 变更 | 内容 |
| --- | --- |
| 新增 `GET /v1/plans/:id` | 返回完整面向用户的套餐规格：流量、有效期、覆盖、功能、网络、价格 |
| 新增 `POST /v1/orders/:orderId/kyc-documents` | 为 INR 的 `pending` 订单上传证件；支持 multipart 上传到 S3 或 JSON URL；响应包含 `esims[]` |
| 修复 `/v1/request` 的数量 | eSIM / 实体 SIM 过去将数量写死为 `1`；现在正确传递数量，`quantity: 4` 会生成 4 张 eSIM |
| `/v1/request` 新增用户信息 | 新增 `hoiUserId`、`kycUserInfo` |
| `/v1/request` 自动识别 INR KYC | INR 账户的 eSIM / 实体 SIM 订单自动设置 `isKycRequired: true`；按证件情况设置 `pending` 或 `submitted` |
| `/v1/request` 响应字段修正 | 业务数据中的 ID 字段为 `_id`，不是 `id`；新增 `quantity`、`kycStatus`、`isKycRequired`、`isKycVerified` |
| `/v1/details/:id` 改为 `esims[]` | 从单个 `esim` 对象改为数组；每张生成的 eSIM 对应一条记录，匹配请求数量 |
| `/v1/details/:id` 完整 eSIM 字段 | 包括 `iccid`、`qrCode`、`lpa`、`smdpAddress`、`msisdn`、`status`、`salePlanName`、`simplyActivationCode`、`snPin`、`snCode`、`configuration_pin` 等 |
| `/v1/details/:id` 新增订单字段 | 新增 `quantity`、`kycStatus`、`isKycRequired`、`isKycVerified` |

## 14. 原文差异与待确认事项

本节是整理者对原文的核对结果与接入问题，不是新增的供应商协议。原文缺少的内容已有部分由公开 Swagger 补充，详见第 16 节；项目实现方案见 [转售 Bot 开发方案](reseller-bot-development.md)。

### 14.1 原文表述不一致或容易混淆之处

| 事项 | 原文情况 | 接入时需要确认的内容 |
| --- | --- | --- |
| 激活 ICCID | 第 12 页标为条件必填，第 13 页放入 Optional，示例提供了 ICCID | 哪些激活场景可以省略 ICCID |
| 充值目标 | 通用表将 `mobile_number` 标为条件必填；分类表把 `mobile_number or iccid` 列为 Optional | 手机号/ICCID 是否至少必填一个、允许的格式 |
| 强制 KYC | 第 7 页说账户启用后必须提供；第 13 页将相关字段放在 Optional 列 | 各账户、各请求类型的实际必填条件 |
| KYC 文件份数 | 账户级强制 KYC 列了三个字段及“最多三个文件”；INR 补交接口明确至少一份 | 账户级 KYC 是否也允许只提交一份 |
| eSIM / 实体 SIM 数量 | 通用表写默认 `1`，分类表写 `quantity` 必填 | 是否允许省略，以及数量上下限 |
| KYC 提交与释放 | 流程图写管理员审核后释放；提交响应在 `isKycVerified: false` 时已包含 eSIM 安装信息 | `submitted` 时能否交付或使用，还是必须等 `verified` |
| 响应包装 | 鉴权/目录通常为 `data`，业务请求为 `data.data`，套餐详情内部也有名为 `data` 的流量规格 | 按接口解析，不能递归剥离所有 `data` |
| `orderId` 含义 | 详情示例为 `DR...`/`AR...`；KYC 响应里的 `orderId` 是 ObjectId，业务编号另放 `distributorOrderId` | 依据具体接口区分数据库 `_id` 与业务展示编号 |
| 多张 eSIM 示例 | 详情示例写 `quantity: 4`，但只展开一条 eSIM | 正文要求真实响应按数量返回，示例不可视为完整数组 |
| 套餐与分销商套餐 ID | 列表同时有 `_id` 与 `distributorPlanId`；请求还有可选 `planId` | 请求 `planId` 及详情中的 `planId` 应对应哪种 ID |
| 品牌 ID | 套餐列表的 `brandId` 是数组，请求字段是单个 ObjectId | 多品牌套餐下应选择哪个品牌 |
| 状态枚举 | 示例中有 `Success`、`pending`、`approved`、`active` | 各业务完整状态枚举、大小写和终态定义 |

### 14.2 原文未给出的能力或规则

以下内容在这份 PDF 中没有找到明确约定；“未记载”不等于供应商一定不支持。

1. **下单幂等键**：没有定义 `Idempotency-Key`、商户订单号去重或重复提交返回同一订单的规则。`hoiUserId` 是用户标识，`notes` 是备注，均未被定义为幂等键。
2. **提交结果不明时的反查**：详情接口要求已拿到 `_id`；未提供按本系统订单号、`hoiUserId` 或备注查询已有订单的接口。
3. **状态推送**：未定义订单 webhook、事件订阅、签名验证或推送重试规则；文档展示的是按 `_id` 查询/轮询。
4. **账户信息**：未列出账户结算币种、强制 KYC 配置、余额的查询接口；也未提供当前账户的实际值。
5. **扣款和价格**：未说明 `activationPrice`、`rechargePrice`、`retailPrice`、`overridePrice` 的优先级，也未说明 `totalAmount` 的金额单位。不能将示例值直接视为本店售价。
6. **取消、退款和补偿**：未列出取消订单、退款或冲正接口，也未说明服务失败后的退款处理。
7. **实体 SIM 物流**：没有完整的收货/配送必填字段、运费、运单号、物流查询接口；“请求创建成功”不能直接证明实体卡已发货或签收。
8. **用量响应**：PDF 未给出 `/esim/usage` 成功响应、计量单位、字段类型、更新时间，以及 `orderId` 对应的具体 ID。公开 Swagger 已补充成功结构和部分单位，见第 16.4 节；真实用量数据仍未验证。
9. **文件与请求限制**：未给出证件文件大小上限、上传超时、下单数量上限、接口限流和推荐轮询间隔。
10. **令牌异常细节**：提供了刷新令牌的正常响应，但未完整列出 refresh token 过期/失效响应以及旧 refresh token 的轮换行为。
11. **多张兑换券结果**：`voucher` 支持可选 `quantity`，但详情示例只出现单个 `voucher` 字符串；多数量的交付结构需要确认。
12. **KYC 拒绝与重提**：未给出审核拒绝状态、拒绝原因字段、重新提交次数或完整生命周期。

### 14.3 本项目需求与配置进度

本小节记录项目接入前提，不属于 PDF 原文：

- 接入范围：eSIM 新购、激活、充值、兑换券、实体 SIM，以及对应的 KYC 和用量查询。
- 业务需求：客户向本店收款渠道付款；确认到账后，机器人向 Commbitz 采购，期望由上游从本店预存余额扣除采购款，再向客户交付商品或处理结果。预存款扣款时点、失败退回规则尚未通过采购验证。
- API 凭据：已完成 Live 正式环境鉴权和只读查询验证；同一组凭据在本次 UAT 探测中被拒绝。凭据及令牌不写入本文。
- 可见套餐：已读取全部 11 个 SKU，含 5 个 eSIM 和 6 个实体 SIM，见第 15 节。
- 价格币种：全部可见套餐的 `pricing.currency.code` 均为 `USD`；账户钱包和实际扣款币种尚无独立验证。
- 套餐级 KYC：11 个套餐的 `documentsRequired` 和 `ekyc` 均为 `false`；分销商账户级强制 KYC 开关仍未确定。
- 店铺人民币售价与上游结算价格需要分开配置；当前 PDF 未给出汇率或加价规则。
- 本次只读验证不代表采购、KYC 提交、实际扣款或机器人收款后自动发货已经联调完成。


## 15. 实际只读联调记录

### 15.1 来源、时间与验证范围

| 项目 | 记录 |
| --- | --- |
| 最后一次套餐及详情快照时间 | 2026-09-15 16:00:55 UTC |
| 验证来源 | 本次会话内多轮实际鉴权、目录查询、套餐详情查询，以及 Live Swagger 定义核对 |
| 实际可用环境 | Live |
| API 基础地址 | `https://api-cb.commbitz.com/distributor-api` |
| 接口说明 | [Live Swagger](https://api-cb.commbitz.com/api/api-distributor) |
| 业务范围 | 仅只读查询；另执行获取/刷新令牌以验证鉴权 |
| 采购请求数量 | `0` |
| 凭据处理 | 本文不保存 API Key、Secret Key、access token 或 refresh token |

本节是账户在此次联调时的可见数据快照，不表示套餐数量、分配权限或配置永久不变。
获取和刷新令牌属于鉴权操作；本次没有调用下单、充值、激活、KYC 文件提交或支付链接创建接口，也没有通过实际采购验证余额扣款行为。

### 15.2 已验证结果

| 检查项 | 实际结果 | 可得出的结论 |
| --- | --- | --- |
| UAT `POST /v1/get-token` | HTTP `401`，`Invalid API credentials` | 同一组凭据在此次 UAT 探测中不可用 |
| Live `POST /v1/get-token` | HTTP `201`，取得 access/refresh token，`expiresIn: 3600` | 凭据可用于 Live 鉴权 |
| Live `POST /v1/refresh-token` | HTTP `201`，响应体 `statusCode: 200` | 令牌刷新成功；HTTP 状态与 PDF 示例的 `200` 有差异 |
| `GET /v1/regional-plan-types` | HTTP `200`，`count: 0` | 此账户此次未返回区域套餐类型；不代表平台没有区域套餐 |
| `GET /v1/countries` | HTTP `200`，`count: 184` | 国家目录查询成功；与 PDF 中 `195` 的示例值不同 |
| `GET /v1/plans` | HTTP `200`，分页总数 `11`，已读取全部页面 | 当前可见套餐共 11 个 |
| `GET /v1/plans/:id` | 11 个套餐详情均为 HTTP `200` | 详情查询成功 |
| 套餐报价币种 | 11 个套餐均为 `pricing.currency.code: "USD"` | 可以确认返回给本账户的套餐报价使用美元 |
| 套餐证件字段 | 11 个套餐均为 `documentsRequired: false` | 此次返回的套餐级证件标记均为关闭 |
| 套餐 eKYC 字段 | 11 个套餐均为 `ekyc: false` | 此次返回的套餐级 eKYC 标记均为关闭 |

### 15.3 全部可见套餐 SKU

以下名称、SKU、类别与 `planIsFor` 保留接口原始值。全部套餐的报价币种为 `USD`，`documentsRequired` 和 `ekyc` 均为 `false`。

| SKU | `simCategory` | `planIsFor` | 接口返回名称 |
| --- | --- | --- | --- |
| `eSim-PB-US-US-50M-30` | `esim` | `6` | ESIM - United States - 50MB - 30 Days |
| `eSim-PB-US-US-50M-30-2` | `esim` | `2` | ESIM - United States - 50MB - 30 Days |
| `eSim-PB-US-US-250M-30-3` | `esim` | `2` | ESIM - United States - 250MB - 30 Days |
| `eSim-PB-US-US-250M-30-1` | `esim` | `6` | ESIM - United States - 250MB - 30 Days |
| `eSim-PB-US-US-250M-30-2` | `esim` | `2` | ESIM - United States - 250MB - 30 Days |
| `pS-BL-US-US-0-30-1` | `physicalsim` | `2` | Physical SIM - United States - Enter Data - 30 Days |
| `pS-PB-US-US-50M-30` | `physicalsim` | `11` | Physical SIM - United States - 50MB - 30 Days |
| `pS-PB-US-US-50M-30-1` | `physicalsim` | `11` | Physical SIM - United States - 50MB - 30 Days |
| `pS-PB-US-US-250M-30-2` | `physicalsim` | `11` | Physical SIM - United States - 250MB - 30 Days |
| `pS-PB-US-US-5-30` | `physicalsim` | `11` | Physical SIM - United States - 250MB - 30 Days |
| `pS-PB-US-US-50M-30-4` | `physicalsim` | `11` | Physical SIM - United States - 250MB - 30 Days |

类别统计：`esim` 共 5 个，`physicalsim` 共 6 个。`simCategory` 是目录字段，提交业务请求时仍应使用 `/v1/request` 定义的 `requestType`。

### 15.4 仍不能从当前查询确认的账户属性

**账户钱包与实际结算币种**

- 已确认的是套餐的 `pricing.currency.code = USD`。
- 当前公开的分销商接口说明中，没有找到单独查询账户钱包余额、钱包币种或账户结算币种的接口。
- 鉴权响应只返回 `accessToken`、`refreshToken`、`expiresIn`；令牌载荷字段仅有 `sub`、`type`、`iat`、`exp`，没有币种或 KYC 配置字段。
- 因此，不将美元报价进一步记为“已经验证账户钱包/扣款币种一定为美元”。需要上游确认，或在明确授权的采购联调中对账验证。

**分销商账户级强制 KYC**

- 已确认的是套餐详情中的 `documentsRequired = false`、`ekyc = false`。
- 这些套餐级字段不能替代第 4 节描述的账户级强制 KYC 设置，也不能直接证明所有实际订单都可免 KYC。
- 当前鉴权、套餐列表、套餐详情及公开分销商接口定义，没有暴露该账户级开关。
- 尚未创建订单，因此没有取得该账户真实订单的 `kycStatus`、`isKycRequired`、`isKycVerified` 以供核对。

### 15.5 实测与原文的新增差异

1. **`planIsFor` 出现未解释的值。** 当前套餐中出现 `6` 和 `11`，而 PDF 及此次读取的 Swagger 查询参数说明只解释了 `0`–`5`。响应字段本身是 number，不能据此判断 `6`、`11` 对应哪类业务。需要上游补充映射。
2. **套餐详情新增证件字段。** Live 实际返回 `documentsRequired` 和 `ekyc`，Swagger 将两者定义为 boolean；PDF 的套餐详情 JSON 示例未列出这两个字段。
3. **刷新令牌的 HTTP 状态不同。** PDF 示例为 HTTP `200`，此次 Live 实际为 HTTP `201`，响应体仍是 `statusCode: 200`。这是已观察到的接口行为差异。
4. **部分 SKU 字面与名称中的流量不一致。** `pS-PB-US-US-5-30`、`pS-PB-US-US-50M-30-4` 的接口名称均写 `250MB`。上述表格保留两者原值，不能仅按 SKU 字符串推导商品规格。
5. **一个实体 SIM 套餐名称仍含占位文字。** `pS-BL-US-US-0-30-1` 的名称含 `Enter Data`，实际可售流量规格需要确认，不将其擅自解释为 0 MB。
6. **公开 Swagger 的接口集合比 PDF 更广。** 例如还有区域目录、设备兼容性、支付链接和短信相关定义。此次仅核对了公开定义；这些额外业务能力没有通过调用验证，也不改变本项目“客户向本店收款渠道付款”的需求。

### 15.6 后续联调边界

已具备继续开发的真实环境、SKU 和套餐报价币种信息。仍待确认或验证的项目包括：

- 本账户的结算/钱包币种，以及账户级强制 KYC 设置。
- `POST /v1/request` 的实际扣款时点、采用的价格字段、余额不足和失败退款规则。
- 下单幂等性，以及请求超时、未收到上游 `_id` 时的查询和核对方式。
- `planIsFor: 6`、`planIsFor: 11` 的含义与允许的请求类型。
- KYC 提交后、审核通过前的 eSIM 交付限制。
- 实体 SIM 物流等第 14 节列出的协议缺口，以及第 16.4 节用量结构的真实响应验证。
- 实际采购测试使用的 SKU、数量与费用上限；当前凭据已验证可用于 Live，采购操作须按正式环境对待。


## 16. 公开 Swagger 与开发补充

### 16.1 可公开读取的定义

本节来自公开 Swagger 的 OpenAPI 定义，不是 PDF 原文，也不等同于所有业务已实测通过。

| 环境 | Swagger 页面 | 定义来源 | 路径数 / 操作数 |
| --- | --- | --- | --- |
| Live | [Live Swagger](https://api-cb.commbitz.com/api/api-distributor) | [Live 初始化定义](https://api-cb.commbitz.com/api/api-distributor/swagger-ui-init.js) | 21 / 21 |
| UAT | [UAT Swagger](https://api-uat.commbitz.com/api/api-distributor) | [UAT 初始化定义](https://api-uat.commbitz.com/api/api-distributor/swagger-ui-init.js) | 22 / 22 |

- 定义读取时间：2026-09-15 16:08:47 UTC。
- 公开定义可不携带账户凭据读取；业务接口按各自鉴权要求调用。
- 两个环境均使用 OpenAPI `3.0.0` 格式，`info.title` 为 `Commbitz API`，`info.version` 为 `1.0`。
- OpenAPI 格式版本、`info.version`、URL 中的 `/v1` 和 PDF 的 `Version 2.0` 含义不同，不据此判定哪个环境“升级”或“降级”。

### 16.2 Live 完整接口目录

下表是此次公开定义中全部 21 个方法与路径。这里的“未执行/未调用”指本项目此次联调的验证范围。

| 方法 | 路径 | 用途 | 本项目状态 |
| --- | --- | --- | --- |
| POST | `/distributor-api/v1/get-token` | 获取令牌 | 已验证 Live |
| POST | `/distributor-api/v1/refresh-token` | 刷新令牌 | 已验证 Live |
| PATCH | `/distributor-api/v1/change-password` | 修改分销商密码 | 账户管理扩展；未执行 |
| GET | `/distributor-api/v1/regions` | 查询区域目录 | 目录扩展；未调用 |
| GET | `/distributor-api/v1/regional-plan-types` | 查询区域套餐类型，旧接口 | 已验证 Live |
| GET | `/distributor-api/v1/countries` | 查询国家目录 | 已验证 Live |
| GET | `/distributor-api/v1/plans` | 查询已分配套餐 | 已验证 Live |
| GET | `/distributor-api/v1/plans/{id}` | 查询套餐规格和价格 | 已验证 Live |
| POST | `/distributor-api/v1/request` | 统一激活、充值、兑换券、eSIM、实体 SIM 请求 | 转售采购核心；未执行 |
| POST | `/distributor-api/v1/order/request-payment-link` | 创建待付款 eSIM 订单并向客户发送上游支付链接 | 与本项目自有收款流程不同；未执行 |
| GET | `/distributor-api/v1/details/{id}` | 查询已创建订单/业务请求 | 转售交付核心；未调用 |
| POST | `/distributor-api/v1/orders/{orderId}/kyc-documents` | 为已有订单提交 KYC 材料 | KYC 流程；未执行 |
| GET | `/distributor-api/esim/usage` | 查询 eSIM 用量 | 售后查询；未调用 |
| GET | `/distributor-api/esim/check-device` | 检查设备 eSIM 兼容性 | 售前扩展；未调用 |
| GET | `/distributor-api/esim/deviceList` | 查询兼容设备列表 | 售前扩展；未调用 |
| GET | `/distributor-api/v1/teltiksms/mdns` | 查询 Teltik 网关 MDN 列表 | 短信扩展；未调用 |
| GET | `/distributor-api/v1/teltiksms/sms` | 查询短信收件箱 | 短信扩展；未调用 |
| GET | `/distributor-api/v1/teltiksms/sms-db` | 查询平台保存的短信日志 | 短信扩展；未调用 |
| POST | `/distributor-api/v1/teltiksms/send` | 发送短信并保存日志 | 短信扩展；未执行 |
| GET | `/distributor-api/v1/buy-iccid/{iccid}` | 按归属本分销商的 ICCID 查询套餐信息 | 已有 SIM 查询；未调用 |
| GET | `/distributor-api/v1/teltiksms/export` | 导出短信 CSV | 短信扩展；未调用 |

### 16.3 UAT 独有的客户定价接口

此次 UAT 比 Live 多出：

```http
PATCH /distributor-api/v1/plans/{planId}/customer-price
```

公开定义的说明要点：

- 这是设置分销商向最终客户展示/收取的价格。
- 该价格独立于 Commbitz 分配给分销商的价格；UAT 说明明确，订单计费取创建订单时当前有效的 `DistributorPlan` 分配价格。
- 路径参数 `planId` 是套餐 `Plan._id`，即套餐列表和 `/v1/plans/:id` 使用的 ID；UAT 说明也将其关联到 `/v1/request`。
- 支持部分更新，至少提供一个价格字段：`overridePrice`、`activationPrice`、`rechargePrice`、`loadedPrice`、`portOutPrice`、`smsPrice`。
- 响应为 `data.planId`、`data.distributorPlanId`、`data.customerPricing`；币种回显分配套餐的币种，不能通过本接口设置。
- UAT 的套餐列表和套餐详情还增加了可空的 `customerPricing` 字段，与 `pricing` 分开；当前 Live 的对应响应 schema 没有该字段。UAT 说明明确它不参与上游订单计费，币种与 `pricing.currency` 一致。
- 定义的错误包括：`400` 参数/价格无效，`403` 调用者类型不适用，`404` 套餐不存在或未分配。

**本项目处理：** 客户向机器人付款的人民币售价先由本地商品配置管理。当前 Live 定义没有此路径，不依赖它实现本店定价。上述计价规则来自 UAT 定义，仍需在 Live 采购对账时验证实际成本、币种和扣款行为。

### 16.4 Swagger 已补充的 eSIM 用量响应

`GET /distributor-api/esim/usage` 的公开定义提供了成功响应结构，补充了 PDF 第 9 节的空缺。以下只是接口定义，尚无本账户真实 eSIM 用量样本。

| 字段路径 | 类型 / 单位 | 说明 |
| --- | --- | --- |
| `statusCode` | number | 外层状态，成功示例为 `200` |
| `data.code` | string | 业务状态，成功示例为 `000` |
| `data.message` | string | 业务说明 |
| `data.data.effectiveTime` / `expiryTime` | string，可空 | 格式化的生效/到期时间 |
| `data.data.effTime` / `expTime` | string，可空 | 原始时间值；示例形如毫秒时间戳，实际单位仍应验证 |
| `data.data.totalUsage` | number | 总用量字段，优先使用下列带单位的字段解释数值 |
| `data.data.totalUsageFormatted` | string | 格式化总用量 |
| `data.data.totalUsageBytes` | number，字节 | 总用量 |
| `data.data.totalUsageMB` / `totalUsageGB` | number，MB / GB | 总用量换算 |
| `data.data.dailyUsage[]` | array | 每日用量及区域 |
| `dailyUsage[].date` / `dateRaw` | string | 格式化日期 / 原始日期，原始示例为 `YYYYMMDD` |
| `dailyUsage[].usage` | string | 格式化用量 |
| `dailyUsage[].usageBytes` / `usageMB` / `usageGB` | number | 对应单位的每日用量 |
| `dailyUsage[].region` | object | `mcc`、`mnc`、`country` |
| `data.data.summary` | object | 天数、平均每日用量、最高/最低日用量及 MB 数值 |
| `data.totalData` | number，可空，MB | 套餐流量额度 |
| `data.totalDataFormatted` | string，可空 | 格式化套餐额度 |
| `data.profileInfo` | object，可空 | 安装状态、设备信息、网络信息及安全字段 |
| `data.enhancedData` | object，可空 | 合并的额度、用量、设备和安全信息 |

`summary` 包括 `totalDays`、`averageDailyUsage`、`averageDailyUsageMB`、`highestUsageDay`、`highestUsageDayMB`、`lowestUsageDay`、`lowestUsageDayMB`。

`profileInfo` 包括 `state`、`profileType`、`eid`、`imei`、`device`、`clientIp`、`mcc`、`cfCode`、`apnExplain`、`pin1`、`pin2`、`puk1`、`puk2`。

`enhancedData` 包括 `totalUsageFormatted`、`totalData`、`totalDataFormatted`、`deviceInfo`、`securityInfo`。开发时只向订单所有者展示必要信息，PIN/PUK、设备标识等不进入普通日志或群聊。

### 16.5 创建请求的补充定义和剩余差异

- Live `/v1/request` 的 OpenAPI 请求体只列出了 `multipart/form-data` schema，但描述明确支持 JSON 客户端使用同名字段。JSON 和 multipart 都应进行合同测试及对应业务联调。
- multipart 的 `quantity` 是整数字符串；`kycUserInfo` 是 JSON 字符串，不能照搬 JSON 请求中的对象编码方式。
- `kycUserInfo` 的 Swagger 说明比 PDF 多出 `address1`、`address2`、`address3`，并允许 `firstName` + `lastName` 合并为 `name`。
- 创建请求响应的 `data.data.currency` 被描述为分销商币种，适用于 eSIM/实体 SIM。后续有授权采购样本时可以核对该字段；当前仍未实际读取订单响应，因此不把它当作本账户币种的既成事实。
- 响应定义解释 `totalAmount`：激活/充值按 `distributorPrice × days`；eSIM/实体 SIM 为订单总额。金额单位、各业务最终取哪个价格和钱包扣款时点仍未完整定义。
- 描述文字还提到 `esimCode`、`phone`，而结构化请求字段采用 `iccid`、`mobile_number`；应记录并通过实际业务确认兼容关系。
- `orderCode` 被描述为历史订单引用，没有定义为本系统订单的幂等键。
- Live 套餐价格 schema 还包括 `loadedPrice`、`portOutPrice`、`smsPrice`；它们不是已经验证可直接用于本店采购计价的通用字段。
- Live/UAT 的公开定义仍未给出 `planIsFor: 6/11` 的含义，也未补齐本账户钱包查询、下单幂等性和完整退款/物流流程。

### 16.6 对转售 Bot 开发的结论

已有信息足以实现并测试转售 Bot 的正常流程：本店收款确认、上游采购请求、按 `_id` 查询交付结果、持久化货品、向买家私信，以及各类业务的输入和 KYC 处理。

采购结果不明必须有人工核对入口；真实自动售卖启用前，仍需完成受控采购、扣款对账、交付和异常恢复验收。具体模块、状态和分阶段交付方案见 [转售 Bot 开发方案](reseller-bot-development.md)。
