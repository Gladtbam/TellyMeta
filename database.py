'''
数据库操作 API
'''
import os
import logging
from datetime import datetime, timedelta
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, select, delete, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
# from sqlalchemy.orm import sessionmaker, relationship
from loadconfig import init_config

config = init_config()

if config.dataBase.dataBaseType == 'sqlite':
    engine = create_async_engine('sqlite+aiosqlite:///embybot.db')
    if os.path.exists('/var/lib/jellyfin/data/playback_reporting.db'):
        only_read_engine = create_async_engine('sqlite+aiosqlite:///var/lib/jellyfin/data/playback_reporting.db')
    else:
        logging.error('Playback Reporting Database not found')
        # exit(1)
else:
    engine = create_async_engine(f'mysql+asyncmy://{config.dataBase.user}:{config.dataBase.password}@{config.dataBase.host}:{config.dataBase.port}/{config.dataBase.databaseName}')

Base = declarative_base()
class User(Base):
    '''用户表'''
    __tablename__ = 'Users'
    TelegramId = Column(String(20), primary_key=True)
    Score = Column(Integer, default=0)
    Checkin = Column(Integer, default=0)
    Warning = Column(Integer, default=0)
    LastCheckin = Column(DateTime, default=datetime.now().date())
    def __repr__(self):
        return f'<User(TelegramId={self.TelegramId}, Score={self.Score}, Checkin={self.Checkin}, Warning={self.Warning}, LastCheckin={self.LastCheckin})>'

class Emby(Base):
    '''Emby表'''
    __tablename__ = 'Emby'
    TelegramId = Column(String(20), ForeignKey('Users.TelegramId'), primary_key=True)
    EmbyId = Column(Text)
    EmbyName = Column(Text)
    LimitDate = Column(DateTime)
    Ban = Column(Boolean, default=False)
    deleteDate = Column(DateTime)
    def __repr__(self):
        return f'<Emby(TelegramId={self.TelegramId}, EmbyId={self.EmbyId}, EmbyName={self.EmbyName}, LimitDate={self.LimitDate}, Ban={self.Ban}, deleteDate={self.deleteDate})>'

class Code(Base):
    '''注册码/续期码 表'''
    __tablename__ = 'Codes'
    CodeId = Column(String(255), primary_key=True)
    TimeStamp = Column(Text)
    Tag = Column(Text)
    def __repr__(self):
        return f'<Code(CodeId={self.CodeId}, TimeStamp={self.TimeStamp}), Tag={self.Tag}>'

async def init_db():
    '''初始化数据库'''
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

async def get_user(TelegramId):
    '''获取用户信息'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                user = await session.get(User, TelegramId)
                if user is not None:
                    session.expunge(user)
                    return user
                else:
                    return None
            except ImportError as e:
                logging.error('Error occurred while getting user %s: %s', TelegramId, e)
                await session.rollback()
                return None

async def create_users(TelegramId):
    '''创建用户'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                if await session.get(User, TelegramId) is None:
                    user = User(TelegramId=TelegramId)
                    session.add(user)
                    await session.commit()
                    return True
                else:
                    return False
            except ImportError as e:
                logging.error('Error occurred while creating user %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def delete_user(TelegramId):
    '''删除用户'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                user = await session.get(User, TelegramId)
                if user is None:
                    return False
                else:
                    await session.delete(user)
                    await session.commit()
                    return True
            except ImportError as e:
                logging.error('Error occurred while deleting user %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def change_score(TelegramId, Score):
    '''更改用户积分'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                user = await session.get(User, TelegramId)
                if user is None:
                    session.add(User(TelegramId=TelegramId, Score=Score))
                else:
                    user.Score += Score
                await session.commit()
                return True
            except ImportError as e:
                logging.error('Error occurred while changing score of user %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def change_checkin_day(TelegramId, Socre=0):
    '''更改用户签到天数'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                user = await session.get(User, TelegramId)
                if user is None:
                    session.add(User(TelegramId=TelegramId, Checkin=1, LastCheckin=datetime.now().date(), Score=Socre))
                else:
                    user.Checkin += 1 # type: ignore
                    user.LastCheckin = datetime.now().date() # type: ignore
                    user.Score += Socre # type: ignore
                await session.commit()
                return True
            except ImportError as e:
                logging.error('Error occurred while changing checkin of user %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def change_warning(TelegramId):
    '''更改用户警告次数'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                user = await session.get(User, TelegramId)
                if user is None:
                    session.add(User(TelegramId=TelegramId, Warning=1, Score=-1))
                else:
                    user.Warning += 1 # type: ignore
                    user.Score -= user.Warning # type: ignore
                await session.commit()
                return True
            except ImportError as e:
                logging.error('Error occurred while changing warning of user %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def get_emby(TelegramId):
    '''获取用户 Emby 信息'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                emby = await session.get(Emby, TelegramId)
                if emby is not None:
                    session.expunge(emby)
                    return  emby
                else:
                    return None
            except ImportError as e:
                logging.error('Error occurred while getting emby %s: %s', TelegramId, e)
                return None

async def create_emby(TelegramId, EmbyId, EmbyName):
    '''创建 Emby 帐户'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                if TelegramId in config.other.adminId:
                    LimitDate = datetime.now() + timedelta(weeks=4752)
                else:
                    LimitDate = datetime.now() + timedelta(days=30)
                if await session.get(Emby, TelegramId) is None:
                    emby = Emby(TelegramId=TelegramId, EmbyId=EmbyId, EmbyName=EmbyName, LimitDate=LimitDate)
                    session.add(emby)
                    await session.commit()
                    return True
                else:
                    return False
            except ImportError as e:
                logging.error('Error occurred while creating emby %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def delete_emby(TelegramId):
    '''删除 Emby 帐户'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                emby = await session.get(Emby, TelegramId)
                if emby is None:
                    return False
                else:
                    await session.delete(emby)
                    await session.commit()
                    return True
            except ImportError as e:
                logging.error('Error occurred while deleting emby %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def limit_emby_ban():
    '''限制到期 Emby 帐户'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                emby = await session.execute(select(Emby).where(Emby.LimitDate < datetime.now().date(), Emby.Ban == False))
                embyIds = []
                for i in emby.scalars():
                    i.Ban = True
                    i.deleteDate = datetime.now().date() + timedelta(days=7)
                    embyIds.append(i.EmbyId)
                await session.commit()
                return embyIds
            except ImportError as e:
                logging.error('Error occurred while limiting emby ban: %s', e)
                await session.rollback()
                return None

async def limit_emby_delete():
    '''删除到期 Emby 帐户'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                emby = await session.execute(select(Emby).where(Emby.deleteDate < datetime.now().date(), Emby.Ban == True))
                embyIds = []
                for i in emby.scalars():
                    await session.delete(i)
                    embyIds.append(i.EmbyId)
                await session.commit()
                return embyIds
            except ImportError as e:
                logging.error('Error occurred while limiting emby delete: %s', e)
                await session.rollback()
                return None

async def update_limit_date(TelegramId, days=30):
    '''更新 Emby 帐户到期时间'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                emby = await session.get(Emby, TelegramId)
                if emby is None:
                    return False
                else:
                    if emby.Ban is True:
                        emby.Ban = False
                        emby.LimitDate = datetime.now().date() + timedelta(days=days)
                    else:
                        emby.LimitDate = emby.LimitDate + timedelta(days=days)
                    await session.commit()
                    return True
            except ImportError as e:
                logging.error('Error occurred while updating limit date of emby %s: %s', TelegramId, e)
                await session.rollback()
                return False

async def create_code(CodeId, TimeStamp, Tag):
    '''写入注册码/续期码'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                session.add(Code(CodeId=CodeId, TimeStamp=TimeStamp, Tag=Tag))
                await session.commit()
                return True
            except ImportError as e:
                logging.error('Error occurred while creating code %s: %s', CodeId, e)
                await session.rollback()
                return False

async def get_code(CodeId):
    '''获取注册码/续期码'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                code = await session.get(Code, CodeId)
                if code is not None:
                    session.expunge(code)
                    return code
                else:
                    return None
            except ImportError as e:
                logging.error('Error occurred while getting code %s: %s', CodeId, e)
                return None

async def delete_code(CodeId):
    '''删除注册码/续期码'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                code = await session.get(Code, CodeId)
                if code is None:
                    return False
                else:
                    await session.delete(code)
                    await session.commit()
                    return True
            except ImportError as e:
                logging.error('Error occurred while deleting code %s: %s', CodeId, e)
                await session.rollback()
                return False

async def delete_limit_code():
    '''删除过期注册码/续期码'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                await session.execute(delete(Code).where(Code.TimeStamp < (datetime.now() - timedelta(days=90)).timestamp()))
                await session.commit()
                return True
            except ImportError as e:
                logging.error('Error occurred while deleting limit code: %s', e)
                await session.rollback()
                return False

async def settle_score(UserRatio, TotalScore):
    '''结算积分'''
    # renewValue = int(await get_renew_value())
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                userScore = {}
                for userId, ratio in UserRatio.items():
                    userValue = int(TotalScore * ratio * 0.5)
                    if userValue < 1:
                        userValue = 1
                    user = await session.get(User, userId)
                    if user is None:
                        session.add(User(TelegramId=userId, Score=userValue))
                    else:
                        # n = user.Score // renewValue
                        # result_score = (userValue - n * renewValue) / (n + 1)
                        # sigma_sum = sum(renewValue / i for i in range(1, n + 1))
                        # userValue = result_score + sigma_sum
                        user.Score += userValue
                    userScore[userId] = userValue
                await session.commit()
                return userScore
            except ImportError as e:
                logging.error('Error occurred while settling score: %s', e)
                await session.rollback()
                return None

async def get_renew_value():
    '''获取续期积分值'''
    async with AsyncSession(engine) as session:
        async with session.begin():
            try:
                renew = await session.execute(select(func.avg(User.Score)).where(User.Score > 10))
                renew_value = renew.scalar()
                if renew_value is None or renew_value < 100:
                    renew_value = 100
                return renew_value
            except ImportError as e:
                logging.error('Error occurred while getting renew value: %s', e)
                await session.rollback()
                return 100
