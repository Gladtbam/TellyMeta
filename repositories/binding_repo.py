from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import LibraryBinding


class BindingRepository:
    """媒体库绑定仓储"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[LibraryBinding]:
        """获取所有媒体库绑定"""
        stmt = select(LibraryBinding)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_key(self, media_id: int, library_name: str) -> LibraryBinding | None:
        """通过 media_id + library_name 组合键获取绑定"""
        stmt = select(LibraryBinding).where(
            LibraryBinding.media_id == media_id,
            LibraryBinding.library_name == library_name,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_media_id(self, media_id: int) -> list[LibraryBinding]:
        """通过 Emby/Jellyfin server_id 获取所有绑定"""
        stmt = select(LibraryBinding).where(LibraryBinding.media_id == media_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_arr_id(self, arr_id: int) -> list[LibraryBinding]:
        """通过 Sonarr/Radarr server_id 获取所有绑定"""
        stmt = select(LibraryBinding).where(LibraryBinding.arr_id == arr_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def save(self, binding: LibraryBinding) -> LibraryBinding:
        """保存（创建或更新）绑定"""
        self.session.add(binding)
        await self.session.commit()
        await self.session.refresh(binding)
        return binding

    async def upsert(
        self,
        media_id: int,
        library_name: str,
        arr_id: int,
        quality_profile_id: int,
        root_folder: str,
    ) -> LibraryBinding:
        """创建或更新绑定（按 media_id + library_name 定位）"""
        binding = await self.get_by_key(media_id, library_name)
        if not binding:
            binding = LibraryBinding(media_id=media_id, library_name=library_name)

        binding.arr_id = arr_id
        binding.quality_profile_id = quality_profile_id
        binding.root_folder = root_folder

        return await self.save(binding)

    async def delete(self, media_id: int, library_name: str) -> bool:
        """删除绑定（按 media_id + library_name 定位）"""
        binding = await self.get_by_key(media_id, library_name)
        if binding:
            await self.session.delete(binding)
            await self.session.commit()
            return True
        return False
