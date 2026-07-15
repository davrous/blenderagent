import { memo } from "react";
import clsx from "clsx";
import type { Gallery, GalleryModel, GalleryTexture } from "../lib/parseMarkdown";

interface Props {
  gallery: Gallery;
  disabled?: boolean;
  onSelectModel: (model: GalleryModel) => void;
  onSelectTexture: (texture: GalleryTexture) => void;
}

function AssetGalleryImpl({ gallery, disabled, onSelectModel, onSelectTexture }: Props) {
  const isTextures = gallery.kind === "textures";
  const title = isTextures
    ? "Pick a texture to apply"
    : "Pick a model to add to the scene";

  return (
    <div className={clsx("asset-gallery", isTextures && "asset-gallery-textures")}>
      <div className="asset-gallery-title">{title}</div>
      <div className="asset-gallery-grid">
        {gallery.kind === "models"
          ? gallery.items.map((model, i) => (
              <button
                key={`${model.modelUrl}-${i}`}
                type="button"
                className="asset-card"
                disabled={disabled}
                title={
                  disabled
                    ? "Please wait for the current turn to finish"
                    : `Add “${model.name}” to the scene`
                }
                onClick={() => onSelectModel(model)}
              >
                <img src={model.imageUrl} alt={model.name} loading="lazy" />
                <span className="asset-card-caption">{model.name}</span>
              </button>
            ))
          : gallery.items.map((texture, i) => (
              <button
                key={`${texture.assetId}-${i}`}
                type="button"
                className="asset-card"
                disabled={disabled}
                title={
                  disabled
                    ? "Please wait for the current turn to finish"
                    : `Apply “${texture.name}” to an object`
                }
                onClick={() => onSelectTexture(texture)}
              >
                <img src={texture.imageUrl} alt={texture.name} loading="lazy" />
                <span className="asset-card-caption">{texture.name}</span>
              </button>
            ))}
      </div>
    </div>
  );
}

export const AssetGallery = memo(AssetGalleryImpl);
