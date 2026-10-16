//! JPEG encoding and downscaling for screenshots and stream frames.

/// Encode tightly packed RGB pixels as JPEG.
pub fn encode_rgb(rgb: &[u8], width: u32, height: u32, quality: u8) -> Option<Vec<u8>> {
    if width == 0 || height == 0 || width > 65535 || height > 65535 {
        return None;
    }
    if rgb.len() < (width as usize) * (height as usize) * 3 {
        return None;
    }
    let mut out = Vec::with_capacity(64 * 1024);
    let encoder = jpeg_encoder::Encoder::new(&mut out, quality.clamp(1, 100));
    encoder
        .encode(rgb, width as u16, height as u16, jpeg_encoder::ColorType::Rgb)
        .ok()?;
    Some(out)
}

/// Shrink to at most `max_width` (keeping the aspect ratio) by averaging the
/// source pixels each target pixel covers, so text stays readable.
pub fn downscale_rgb(rgb: &[u8], width: u32, height: u32, max_width: u32) -> (Vec<u8>, u32, u32) {
    if width <= max_width || max_width == 0 {
        return (rgb.to_vec(), width, height);
    }
    let new_w = max_width;
    let new_h = ((height as u64 * new_w as u64) / width as u64).max(1) as u32;
    let mut out = vec![0u8; (new_w * new_h * 3) as usize];
    for ty in 0..new_h {
        let y0 = (ty as u64 * height as u64 / new_h as u64) as u32;
        let y1 = (((ty + 1) as u64 * height as u64 / new_h as u64) as u32).max(y0 + 1).min(height);
        for tx in 0..new_w {
            let x0 = (tx as u64 * width as u64 / new_w as u64) as u32;
            let x1 = (((tx + 1) as u64 * width as u64 / new_w as u64) as u32).max(x0 + 1).min(width);
            let mut acc = [0u32; 3];
            for y in y0..y1 {
                let row = (y * width) as usize * 3;
                for x in x0..x1 {
                    let i = row + x as usize * 3;
                    acc[0] += rgb[i] as u32;
                    acc[1] += rgb[i + 1] as u32;
                    acc[2] += rgb[i + 2] as u32;
                }
            }
            let count = (y1 - y0) * (x1 - x0);
            let o = ((ty * new_w + tx) * 3) as usize;
            for c in 0..3 {
                out[o + c] = (acc[c] / count) as u8;
            }
        }
    }
    (out, new_w, new_h)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_a_valid_jpeg() {
        let jpeg = encode_rgb(&vec![200u8; 32 * 16 * 3], 32, 16, 60).unwrap();
        assert_eq!(&jpeg[..2], &[0xFF, 0xD8]);
        assert_eq!(&jpeg[jpeg.len() - 2..], &[0xFF, 0xD9]);
        assert!(encode_rgb(&[0u8; 3], 10, 10, 60).is_none(), "too few pixels");
    }

    #[test]
    fn downscales_keeping_the_aspect_ratio_and_averages() {
        // 4x2 image: left half black, right half white.
        let mut rgb = Vec::new();
        for _ in 0..2 {
            for x in 0..4 {
                let v = if x < 2 { 0 } else { 255 };
                rgb.extend_from_slice(&[v, v, v]);
            }
        }
        let (out, w, h) = downscale_rgb(&rgb, 4, 2, 2);
        assert_eq!((w, h), (2, 1));
        assert_eq!(out, vec![0, 0, 0, 255, 255, 255]);
        let (same, w, h) = downscale_rgb(&rgb, 4, 2, 1280);
        assert_eq!((w, h, same.len()), (4, 2, rgb.len()));
    }
}
