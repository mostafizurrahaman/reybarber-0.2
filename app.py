from flask import Flask, request, render_template, jsonify, session, send_from_directory, redirect
import os
import base64
from openai import OpenAI
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import json
import uuid
import shutil
import boto3
from botocore.exceptions import ClientError
import tempfile

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-here-change-in-production')

# Configuration
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['TEMP_FOLDER'] = 'temp_uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# Create temp folder if it doesn't exist (for temporary processing)
os.makedirs(app.config['TEMP_FOLDER'], exist_ok=True)

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Initialize S3 client
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    region_name=os.getenv('AWS_REGION')
)
S3_BUCKET = os.getenv('AWS_S3_BUCKET')
S3_PREFIX = 'reference'  # Base prefix for all reference images


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_s3_key(barber_code, filename):
    """Generate S3 key for a reference image"""
    return f"{S3_PREFIX}/{barber_code}/{filename}"


def upload_to_s3(file_path, s3_key):
    """Upload a file to S3"""
    try:
        s3_client.upload_file(file_path, S3_BUCKET, s3_key)
        return True
    except ClientError as e:
        print(f"Error uploading to S3: {str(e)}")
        return False


def download_from_s3(s3_key, local_path):
    """Download a file from S3 to local path"""
    try:
        s3_client.download_file(S3_BUCKET, s3_key, local_path)
        return True
    except ClientError as e:
        print(f"Error downloading from S3: {str(e)}")
        return False


def delete_from_s3(s3_key):
    """Delete a file from S3"""
    try:
        s3_client.delete_object(Bucket=S3_BUCKET, Key=s3_key)
        return True
    except ClientError as e:
        print(f"Error deleting from S3: {str(e)}")
        return False


def list_s3_objects(prefix):
    """List all objects in S3 with given prefix"""
    try:
        objects = []
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            if 'Contents' in page:
                objects.extend(page['Contents'])
        return objects
    except ClientError as e:
        print(f"Error listing S3 objects: {str(e)}")
        return []


def delete_s3_prefix(prefix):
    """Delete all objects with given prefix from S3"""
    try:
        objects = list_s3_objects(prefix)
        if objects:
            delete_keys = [{'Key': obj['Key']} for obj in objects]
            s3_client.delete_objects(
                Bucket=S3_BUCKET,
                Delete={'Objects': delete_keys}
            )
        return True
    except ClientError as e:
        print(f"Error deleting S3 prefix: {str(e)}")
        return False


def get_s3_presigned_url(s3_key, expiration=3600):
    """Generate a presigned URL for S3 object"""
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': s3_key},
            ExpiresIn=expiration
        )
        return url
    except ClientError as e:
        print(f"Error generating presigned URL: {str(e)}")
        return None


def get_s3_public_url(s3_key):
    """Generate public S3 URL for an object"""
    region = os.getenv('AWS_REGION')
    return f"https://{S3_BUCKET}.s3.{region}.amazonaws.com/{s3_key}"


def get_image_url(barber_code, filename):
    """Get full S3 URL for an image"""
    s3_key = get_s3_key(barber_code, filename)
    return get_s3_public_url(s3_key)


def encode_image(image_path):
    """Encode image to base64 from local path"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')


def encode_image_from_s3(s3_key):
    """Download image from S3 and encode to base64"""
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
        image_data = response['Body'].read()
        return base64.b64encode(image_data).decode('utf-8')
    except ClientError as e:
        print(f"Error encoding image from S3: {str(e)}")
        return None


def get_barber_images_from_s3(barber_code, as_urls=True):
    """Get list of reference images for a barber from S3"""
    prefix = f"{S3_PREFIX}/{barber_code}/"
    objects = list_s3_objects(prefix)
    images = []
    for obj in objects:
        filename = obj['Key'].replace(prefix, '')
        if filename and allowed_file(filename):
            if as_urls:
                images.append(get_image_url(barber_code, filename))
            else:
                images.append(filename)
    return images


def get_all_barbers_from_s3(as_urls=True):
    """Get all barbers and their image counts from S3"""
    prefix = f"{S3_PREFIX}/"
    objects = list_s3_objects(prefix)
    
    barbers = {}
    for obj in objects:
        key = obj['Key'].replace(prefix, '')
        parts = key.split('/')
        if len(parts) >= 2:
            barber_code = parts[0]
            filename = parts[1]
            if filename and allowed_file(filename):
                if barber_code not in barbers:
                    barbers[barber_code] = []
                if as_urls:
                    barbers[barber_code].append(get_image_url(barber_code, filename))
                else:
                    barbers[barber_code].append(filename)
    
    return barbers


def describe_hairstyle(image_path=None, s3_key=None):
    """Generate description for a single hairstyle image"""
    try:
        if s3_key:
            base64_image = encode_image_from_s3(s3_key)
        else:
            base64_image = encode_image(image_path)
        
        if not base64_image:
            return "Unable to load image"
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Describe ONLY the haircut/hairstyle in this image. Focus on:
                            - Hair length (short/medium/long)
                            - Cut type (layered, bob, fade, undercut, etc.)
                            - Texture (straight/wavy/curly/coily)
                            - Shape and structure
                            - Styling (how it's arranged)
                            
                            IGNORE: Hair color, face features, skin tone, accessories.
                            Keep it under 40 words, focus only on the haircut structure."""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "low"
                            }
                        }
                    ]
                }
            ],
            max_tokens=100,
            temperature=0.3
        )
        
        return response.choices[0].message.content.strip()
    
    except Exception as e:
        print(f"Error describing image: {str(e)}")
        return "Unable to generate description"


def compare_hairstyles(input_image_path, reference_s3_key):
    """Compare two hairstyle images and return similarity percentage with reasoning"""
    try:
        # Encode input image from local path
        input_base64 = encode_image(input_image_path)
        # Encode reference image from S3
        reference_base64 = encode_image_from_s3(reference_s3_key)
        
        if not input_base64 or not reference_base64:
            return 0, "Unable to load images"
        
        # Ask GPT-4 Vision to compare the hairstyles
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Compare ONLY the haircut/hairstyle structure in these two images. 
                            
                            Focus ONLY on:
                            - Hair length match
                            - Cut/style type similarity (layers, bob, fade, etc.)
                            - Texture similarity (straight/wavy/curly)
                            - Overall shape and structure
                            - Styling approach
                            
                            COMPLETELY IGNORE:
                            - Hair color or highlights
                            - Face features (eyes, nose, skin, etc.)
                            - Accessories
                            - Background
                            
                            Respond with ONLY this JSON format (no extra text):
                            {"similarity": 85, "reason": "Both have medium-length layered cuts with similar texture"}
                            
                            Where similarity is 0-100 based purely on haircut structure match."""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{input_base64}",
                                "detail": "low"
                            }
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{reference_base64}",
                                "detail": "low"
                            }
                        }
                    ]
                }
            ],
            max_tokens=150,
            temperature=0.3
        )
        
        # Parse the response
        result_text = response.choices[0].message.content.strip()
        
        # Try to extract JSON if there's extra text
        if '```' in result_text:
            result_text = result_text.split('```json')[1].split('```')[0].strip()
        elif '```' in result_text:
            result_text = result_text.split('```')[1].split('```')[0].strip()
        
        # Remove any markdown or extra formatting
        result_text = result_text.replace('```json', '').replace('```', '').strip()
        
        print(f"API Response: {result_text}")  # Debug log
        
        result = json.loads(result_text)
        return result['similarity'], result.get('reason', 'Comparison completed')
    
    except json.JSONDecodeError as e:
        print(f"JSON parsing error: {str(e)}")
        print(f"Response was: {result_text}")
        return 0, "Unable to parse comparison result"
    except Exception as e:
        print(f"Error comparing images: {str(e)}")
        return 0, f"Error: {str(e)}"


@app.route('/')
def index():
    print("Welcome To Reybarber")


@app.route('/upload_reference', methods=['POST'])
def upload_reference():
    """Handle reference image uploads"""
    if 'images' not in request.files:
        return jsonify({'error': 'No images provided'}), 400
    
    # Get barber_code from form data
    barber_code = request.form.get('barber_code')
    if not barber_code:
        return jsonify({'error': 'barber_code is required'}), 400
    
    # Sanitize barber_code to prevent path traversal
    barber_code = secure_filename(barber_code)
    if not barber_code:
        return jsonify({'error': 'Invalid barber_code'}), 400
    
    files = request.files.getlist('images')
    
    if len(files) == 0:
        return jsonify({'error': 'No files selected'}), 400
    
    uploaded_files = []
    
    for file in files:
        if file.filename == '':
            continue
            
        if not allowed_file(file.filename):
            continue
        
        try:
            # Generate unique filename
            unique_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
            
            # Save temporarily
            temp_path = os.path.join(app.config['TEMP_FOLDER'], unique_filename)
            file.save(temp_path)
            
            # Upload to S3
            s3_key = get_s3_key(barber_code, unique_filename)
            if upload_to_s3(temp_path, s3_key):
                uploaded_files.append(unique_filename)
            
            # Clean up temp file
            os.remove(temp_path)
            
        except Exception as e:
            print(f"Error uploading file: {str(e)}")
    
    if len(uploaded_files) == 0:
        return jsonify({'error': 'No valid images uploaded'}), 400
    
    # Get total images count for this barber from S3 with URLs
    all_images = get_barber_images_from_s3(barber_code, as_urls=True)
    
    # Convert uploaded files to URLs
    uploaded_with_urls = [get_image_url(barber_code, f) for f in uploaded_files]
    
    return jsonify({
        'success': True,
        'barber_code': barber_code,
        'uploaded_count': len(uploaded_files),
        'total_images': len(all_images),
        'files': uploaded_with_urls
    })


@app.route('/get_barbers', methods=['GET'])
def get_barbers():
    """Get all barbers and their reference images"""
    barbers_data = get_all_barbers_from_s3()
    
    barbers = []
    for barber_code, images in barbers_data.items():
        if len(images) > 0:
            barbers.append({
                'barber_code': barber_code,
                'reference_images': images,
                'image_count': len(images)
            })
    
    return jsonify({
        'success': True,
        'barbers': barbers,
        'total_barbers': len(barbers)
    })


@app.route('/get_barber/<barber_code>', methods=['GET'])
def get_barber(barber_code):
    """Get a specific barber's reference images"""
    # Sanitize barber_code
    barber_code = secure_filename(barber_code)
    if not barber_code:
        return jsonify({'error': 'Invalid barber_code'}), 400
    
    images = get_barber_images_from_s3(barber_code, as_urls=True)
    
    if not images:
        return jsonify({
            'success': False,
            'error': 'Barber not found'
        }), 404
    
    return jsonify({
        'success': True,
        'barber_code': barber_code,
        'reference_images': images,
        'image_count': len(images)
    })


@app.route('/analyze', methods=['POST'])
def analyze():
    """Handle input image upload and comparison with a specific barber"""
    # Check if file is in request
    if 'image' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400
    
    # Get barber_code from form data
    barber_code = request.form.get('barber_code')
    if not barber_code:
        return jsonify({'error': 'barber_code is required'}), 400
    
    # Sanitize barber_code
    barber_code = secure_filename(barber_code)
    if not barber_code:
        return jsonify({'error': 'Invalid barber_code'}), 400
    
    file = request.files['image']
    
    # Check if file is selected
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Check if file type is allowed
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type. Allowed: PNG, JPG, JPEG, GIF, WEBP'}), 400
    
    try:
        # Check if reference images exist in S3
        reference_images = get_barber_images_from_s3(barber_code, as_urls=False)
        
        if not reference_images:
            return jsonify({
                'error': 'Please upload reference images first for this barber'
            }), 400
        
        # Save the uploaded input file temporarily
        input_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
        input_filepath = os.path.join(app.config['TEMP_FOLDER'], input_filename)
        file.save(input_filepath)
        
        # Get description of input image
        input_description = describe_hairstyle(image_path=input_filepath)
        
        # Compare with each reference image
        results = []
        for ref_image in reference_images:
            ref_s3_key = get_s3_key(barber_code, ref_image)
            
            # Get description of reference image from S3
            ref_description = describe_hairstyle(s3_key=ref_s3_key)
            
            # Compare the two images
            similarity, reason = compare_hairstyles(input_filepath, ref_s3_key)
            
            results.append({
                'image': get_image_url(barber_code, ref_image),
                'similarity': round(similarity, 1),
                'description': ref_description,
                'reason': reason
            })
        
        # Sort by similarity (highest first)
        results.sort(key=lambda x: x['similarity'], reverse=True)
        
        # Clean up input file
        os.remove(input_filepath)
        
        return jsonify({
            'success': True,
            'barber_code': barber_code,
            'input_description': input_description,
            'matches': results
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/analyze_all', methods=['POST'])
def analyze_all():
    """Analyze input image against ALL barbers' reference images"""
    # Check if file is in request
    if 'image' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400
    
    file = request.files['image']
    
    # Check if file is selected
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Check if file type is allowed
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type. Allowed: PNG, JPG, JPEG, GIF, WEBP'}), 400
    
    try:
        # Save the uploaded input file temporarily
        input_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
        input_filepath = os.path.join(app.config['TEMP_FOLDER'], input_filename)
        file.save(input_filepath)
        
        # Get description of input image
        input_description = describe_hairstyle(image_path=input_filepath)
        
        # Get all barbers from S3
        barbers_data = get_all_barbers_from_s3(as_urls=False)
        
        all_barber_results = []
        
        for barber_code, reference_images in barbers_data.items():
            if len(reference_images) == 0:
                continue
            
            # Compare with each reference image for this barber
            barber_matches = []
            for ref_image in reference_images:
                ref_s3_key = get_s3_key(barber_code, ref_image)
                
                # Get description of reference image from S3
                ref_description = describe_hairstyle(s3_key=ref_s3_key)
                
                # Compare the two images
                similarity, reason = compare_hairstyles(input_filepath, ref_s3_key)
                
                barber_matches.append({
                    'image': get_image_url(barber_code, ref_image),
                    'similarity': round(similarity, 1),
                    'description': ref_description,
                    'reason': reason
                })
            
            # Sort matches by similarity (highest first)
            barber_matches.sort(key=lambda x: x['similarity'], reverse=True)
            
            # Calculate average similarity for this barber
            avg_similarity = sum(m['similarity'] for m in barber_matches) / len(barber_matches) if barber_matches else 0
            
            # Get best match for this barber
            best_match = barber_matches[0] if barber_matches else None
            
            all_barber_results.append({
                'barber_code': barber_code,
                'average_similarity': round(avg_similarity, 1),
                'best_match': best_match,
                'all_matches': barber_matches,
                'total_comparisons': len(barber_matches)
            })
        
        # Sort barbers by average similarity (highest first)
        all_barber_results.sort(key=lambda x: x['average_similarity'], reverse=True)
        
        # Clean up input file
        os.remove(input_filepath)
        
        # Get the best overall match
        best_overall = None
        if all_barber_results:
            for barber_result in all_barber_results:
                if barber_result['best_match']:
                    if best_overall is None or barber_result['best_match']['similarity'] > best_overall['similarity']:
                        best_overall = {
                            'barber_code': barber_result['barber_code'],
                            **barber_result['best_match']
                        }
        
        return jsonify({
            'success': True,
            'input_description': input_description,
            'best_overall_match': best_overall,
            'barber_results': all_barber_results,
            'total_barbers_compared': len(all_barber_results)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/reference_image/<barber_code>/<filename>')
def serve_reference_image(barber_code, filename):
    """Serve reference images from S3 (redirect to presigned URL)"""
    # Sanitize inputs
    barber_code = secure_filename(barber_code)
    filename = secure_filename(filename)
    
    if not barber_code or not filename:
        return jsonify({'error': 'Invalid parameters'}), 400
    
    s3_key = get_s3_key(barber_code, filename)
    
    # Check if object exists
    try:
        s3_client.head_object(Bucket=S3_BUCKET, Key=s3_key)
    except ClientError:
        return jsonify({'error': 'Image not found'}), 404
    
    # Generate presigned URL and redirect
    presigned_url = get_s3_presigned_url(s3_key)
    if presigned_url:
        return redirect(presigned_url)
    else:
        return jsonify({'error': 'Unable to generate image URL'}), 500


@app.route('/delete_barber/<barber_code>', methods=['DELETE'])
def delete_barber(barber_code):
    """Delete a barber and all their reference images"""
    # Sanitize barber_code
    barber_code = secure_filename(barber_code)
    if not barber_code:
        return jsonify({'error': 'Invalid barber_code'}), 400
    
    # Check if barber exists and get images before deletion
    images = get_barber_images_from_s3(barber_code, as_urls=True)
    if not images:
        return jsonify({
            'success': False,
            'error': 'Barber not found'
        }), 404
    
    try:
        prefix = f"{S3_PREFIX}/{barber_code}/"
        if delete_s3_prefix(prefix):
            return jsonify({
                'success': True,
                'message': f'Barber {barber_code} deleted successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to delete barber'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/delete_image/<barber_code>/<filename>', methods=['DELETE'])
def delete_image(barber_code, filename):
    """Delete a specific reference image"""
    # Sanitize inputs
    barber_code = secure_filename(barber_code)
    filename = secure_filename(filename)
    
    if not barber_code or not filename:
        return jsonify({'error': 'Invalid parameters'}), 400
    
    s3_key = get_s3_key(barber_code, filename)
    
    # Check if object exists
    try:
        s3_client.head_object(Bucket=S3_BUCKET, Key=s3_key)
    except ClientError:
        return jsonify({
            'success': False,
            'error': 'Image not found'
        }), 404
    
    try:
        if delete_from_s3(s3_key):
            return jsonify({
                'success': True,
                'message': f'Image {filename} deleted successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to delete image'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/clear_session', methods=['GET', 'POST'])
def clear_session():
    """Clear all session data (legacy endpoint for compatibility)"""
    session.clear()
    return jsonify({'success': True, 'message': 'Session cleared'})


@app.route('/clear_all', methods=['POST', 'DELETE'])
def clear_all():
    """Clear ALL barber data - USE WITH CAUTION"""
    try:
        # Get all barbers first
        barbers_data = get_all_barbers_from_s3()
        deleted_barbers = list(barbers_data.keys())
        
        # Delete all objects with the reference prefix
        if delete_s3_prefix(f"{S3_PREFIX}/"):
            return jsonify({
                'success': True,
                'message': 'All barber data cleared',
                'deleted_barbers': deleted_barbers,
                'total_deleted': len(deleted_barbers)
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to clear all data'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'AI Hairstyle Matcher'
    })


if __name__ == '__main__':
    # Check if API key is set
    if not os.getenv('OPENAI_API_KEY'):
        print("WARNING: OPENAI_API_KEY environment variable is not set!")
        print("Set it in .env file: OPENAI_API_KEY='your-api-key-here'")
    
    # Check if S3 credentials are set
    if not os.getenv('AWS_ACCESS_KEY_ID') or not os.getenv('AWS_SECRET_ACCESS_KEY'):
        print("WARNING: AWS S3 credentials are not set!")
        print("Set them in .env file:")
        print("  AWS_ACCESS_KEY_ID='your-access-key'")
        print("  AWS_SECRET_ACCESS_KEY='your-secret-key'")
        print("  AWS_REGION='your-region'")
        print("  AWS_S3_BUCKET='your-bucket-name'")
    
    print("\nAI Hairstyle Matcher - S3 Based Storage")
    print("Reference images are stored in S3 bucket\n")
    
    app.run(debug=True, host='0.0.0.0', port=8080)