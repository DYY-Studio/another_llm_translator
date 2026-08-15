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

export function moveFilesByCommand(
  fileIds: string[],
  movedFileIds: string[],
  command: FileMoveCommand,
) {
  const moved = new Set(movedFileIds);
  if (
    moved.size === 0
    || moved.size !== movedFileIds.length
    || movedFileIds.some((fileId) => !fileIds.includes(fileId))
  ) return fileIds;

  const orderedMoved = fileIds.filter((fileId) => moved.has(fileId));
  const remaining = fileIds.filter((fileId) => !moved.has(fileId));
  if (remaining.length === 0) return fileIds;

  if (command === "top") {
    return moveFileBlock(fileIds, orderedMoved, remaining[0], "before");
  }
  if (command === "bottom") {
    return moveFileBlock(fileIds, orderedMoved, remaining[remaining.length - 1], "after");
  }

  const firstSelectedIndex = fileIds.indexOf(orderedMoved[0]);
  const lastSelectedIndex = fileIds.indexOf(orderedMoved[orderedMoved.length - 1]);
  if (command === "up") {
    return firstSelectedIndex <= 0
      ? fileIds
      : moveFileBlock(fileIds, orderedMoved, fileIds[firstSelectedIndex - 1], "before");
  }
  return lastSelectedIndex >= fileIds.length - 1
    ? fileIds
    : moveFileBlock(fileIds, orderedMoved, fileIds[lastSelectedIndex + 1], "after");
}
