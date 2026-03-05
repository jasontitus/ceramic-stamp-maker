import cv2
import numpy as np
import subprocess
import os

def extract_and_convert(image_path):
    # Read image in grayscale
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    
    if img is None:
        print(f"Error: Could not find {image_path}")
        return

    # Apply binary threshold for high contrast
    _, thresh = cv2.threshold(img, 150, 255, cv2.THRESH_BINARY_INV)
    
    # Define the region of interest: left column of the page
    h, w = thresh.shape
    left_roi = thresh[:, :int(w * 0.45)]
    
    # Sum pixel values horizontally to find where the rows are
    horizontal_sum = np.sum(left_roi, axis=1)
    
    # Find row boundaries
    rows = []
    in_row = False
    start_y = 0
    
    for y, val in enumerate(horizontal_sum):
        if val > 0 and not in_row:
            in_row = True
            start_y = y
        elif val == 0 and in_row:
            in_row = False
            # Filter out tiny specks of noise
            if y - start_y > 10: 
                rows.append((start_y, y))
                
    # The leftmost motif generally falls within the first 15% of the total image width
    motif_width = int(w * 0.15) 
    
    for i, (start_y, end_y) in enumerate(rows):
        pad = 8
        y1 = max(0, start_y - pad)
        y2 = min(h, end_y + pad)
        
        # Crop the leftmost section
        row_crop = img[y1:y2, 10:motif_width] 
        _, final_crop = cv2.threshold(row_crop, 150, 255, cv2.THRESH_BINARY)
        
        bmp_filename = f"pattern_row_{i+1:02d}.bmp"
        svg_filename = f"pattern_row_{i+1:02d}.svg"
        
        # Save temporary BMP
        cv2.imwrite(bmp_filename, final_crop)
        
        # Call potrace to convert BMP directly to SVG
        try:
            subprocess.run(["potrace", bmp_filename, "-s", "-o", svg_filename], check=True)
            print(f"Successfully generated {svg_filename}")
        except FileNotFoundError:
            print("Error: Potrace is not installed. Please run 'brew install potrace' in your terminal.")
            return
        except subprocess.CalledProcessError as e:
            print(f"Error generating SVG for row {i+1}: {e}")
            
        # Clean up the temporary BMP file
        if os.path.exists(bmp_filename):
            os.remove(bmp_filename)

if __name__ == "__main__":
    # Ensure your image is named correctly and in the same directory
    extract_and_convert("IMG_3570.JPG")
