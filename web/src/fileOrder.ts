export type DropPosition = "before" | "after";

export function moveFileBlock(
  fileIds: string[],
  movedFileIds: string[],
  targetFileId: string,
  position: DropPosition,
) {
  const moved = new Set(movedFileIds);
  if (
    moved.size === 0
    || moved.size !== movedFileIds.length
    || !fileIds.includes(targetFileId)
    || moved.has(targetFileId)
    || movedFileIds.some((fileId) => !fileIds.includes(fileId))
  ) return fileIds;

  const orderedMoved = fileIds.filter((fileId) => moved.has(fileId));
  const remaining = fileIds.filter((fileId) => !moved.has(fileId));
  const targetIndex = remaining.indexOf(targetFileId);
  if (targetIndex < 0) return fileIds;
  const insertionIndex = targetIndex + (position === "after" ? 1 : 0);
  return [
    ...remaining.slice(0, insertionIndex),
    ...orderedMoved,
    ...remaining.slice(insertionIndex),
  ];
}
