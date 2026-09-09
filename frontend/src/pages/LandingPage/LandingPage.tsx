import { useEffect } from 'react'

const PUBLIC_SITE_PATH =
  import.meta.env.DEV ? '/marketing-site/' : '/'

export default function LandingPage() {
  useEffect(() => {
    window.location.replace(
      PUBLIC_SITE_PATH,
    )
  }, [])

  return (
    <div
      style={{
        minHeight: '100vh',
        background: '#0A0F14',
        color: '#F2F6F8',

        fontFamily:
          '"PingFang SC", "Microsoft YaHei", sans-serif',

        display: 'flex',
        flexDirection: 'column',

        alignItems: 'center',
        justifyContent: 'center',

        gap: 18,

        padding: 24,

        textAlign: 'center',
      }}
    >
      <h1
        style={{
          margin: 0,

          fontSize: 28,
          fontWeight: 800,
        }}
      >
        盘迹
      </h1>

      <p
        style={{
          margin: 0,

          color: '#98A1B3',

          fontSize: 14,
          lineHeight: 1.7,
        }}
      >
        门户加载异常，请选择访问入口。
      </p>

      <nav
        style={{
          display: 'flex',

          gap: 10,

          flexWrap: 'wrap',
          justifyContent: 'center',
        }}
      >
        <a
          href={PUBLIC_SITE_PATH}
          style={{
            color: '#041611',

            background: '#00F6C2',

            padding: '10px 18px',

            borderRadius: 10,

            textDecoration: 'none',

            fontWeight: 700,
          }}
        >
          盘迹首页
        </a>

        <a
          href="/login"
          style={{
            color: '#F2F6F8',

            background: '#161F29',

            border:
              '1px solid #263440',

            padding: '10px 18px',

            borderRadius: 10,

            textDecoration: 'none',
          }}
        >
          登录盘迹
        </a>

        <a
          href="/portal/index.html"
          style={{
            color: '#F2F6F8',

            background: '#161F29',

            border:
              '1px solid #263440',

            padding: '10px 18px',

            borderRadius: 10,

            textDecoration: 'none',
          }}
        >
          使用说明
        </a>
      </nav>
    </div>
  )
}
