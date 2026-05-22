import asyncio
import asyncpg

async def test():
    try:
        conn = await asyncpg.connect(
            host="aws-1-eu-central-1.pooler.supabase.com",
            port=5432,
            user="postgres.zfdikybpktsqjaiffgfe",
            password="@$159357Ayomikun",
            database="postgres",
        )
        version = await conn.fetchval("SELECT version()")
        print(f"Connected! {version}")
        await conn.close()
    except Exception as e:
        print(f"Connection failed: {e}")

asyncio.run(test())
