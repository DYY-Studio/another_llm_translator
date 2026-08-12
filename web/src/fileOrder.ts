export type DropPosition = "before" | "after";
export type FileMoveCommand = "top" | "up" | "down" | "bottom";

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

export function moveFileByCommand(
  fileIds: string[],
  fileId: string,
  command: FileMoveCommand,
) {
  const index = fileIds.indexOf(fileId);
  if (index < 0) return fileIds;
  if (command === "top") {
    return index === 0
      ? fileIds
      : moveFileBlock(fileIds, [fileId], fileIds[0], "before");
  }
  if (command === "up") {
    return index === 0
      ? fileIds
      : moveFileBlock(fileIds, [fileId], fileIds[index - 1], "before");
  }
  if (command === "down") {
    return index === fileIds.length - 1
      ? fileIds
      : moveFileBlock(fileIds, [fileId], fileIds[index + 1], "after");
  }
  return index === fileIds.length - 1
    ? fileIds
    : moveFileBlock(fileIds, [fileId], fileIds[fileIds.length - 1], "after");
}
